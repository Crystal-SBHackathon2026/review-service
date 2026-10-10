"""검토를 통과한 명세를 gitops 레포의 환경별 overlay 로 커밋한다.

검토 워커 그래프의 마지막 노드다. verdict 가 pass 일 때만 돈다.

    ① verdict.applied_ops(state) 로 수정 지시서를 모은다
    ② spec_ref 의 커밋에서 원본 deploy.yaml 을 읽는다 (가린 사본이 아니라 원본)
    ③ ops 를 적용하고 render_overlay() 로 overlay 파일을 만든다
    ④ blocking 경고가 없으면 gitops 에 커밋한다

이미지 태그는 넣지 않는다. overlay 는 설정만 담고 이미지는 CI 가 base 의 newTag 로 정한다.
병합 SHA 도 여기서 기록하지 않는다 — 워커 merge_pr 이 업무 DB 에 저장한다.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol

import yaml
from pydantic import ValidationError

from review_ai.overlay import RenderedOverlay, render_overlay
from review_ai.patching import apply_ops
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.state import DeployResult, ReviewState
from review_ai.verdict import applied_ops
from review_common.github import ProtectedFileRemoval, is_protected

__all__ = ["GitClient", "make_commit_overlay", "make_overlay_guard", "commit_message", "removed_protected",
           "PROTECTED_OVERLAY_FILES", "OVERLAY_RESOURCE_REMOVED"]

# 지금 배포에 있는데 렌더 결과에서 사라지면 안 되는 overlay 파일(fnmatch 패턴). 사라지면 Argo CD prune 이 실제 리소스를 지운다.
# 10/09 sample-app#11: network: {} 명세 → ingress.yaml 삭제 → ALB 삭제(10분 장애, 주소 변경).
# pvc-*.yaml: 명세에서 persistent 볼륨을 빼거나 이름을 바꾸면 PVC 가 지워진다. local-path(local)·GKE 기본
# StorageClass 는 reclaim Delete 라 데이터(SQLite 파일)도 같이 사라진다. 일부러 지우려면 사람이 gitops 에서 지운다.
PROTECTED_OVERLAY_FILES: tuple[str, ...] = ("ingress.yaml", "pvc-*.yaml", "service-public.yaml")
OVERLAY_RESOURCE_REMOVED = "OVERLAY_RESOURCE_REMOVED"


class GitClient(Protocol):
    """커밋 노드가 쓰는 Git 접근. 네트워크 오류는 구현체가 TransientError 로 올린다."""

    async def read_file(self, repository: str, path: str, ref: str) -> str:
        """앱 레포의 특정 커밋에서 파일 내용을 읽는다. 없으면 FileNotFoundError."""
        ...

    async def list_files(self, directory: str) -> list[str]:
        """gitops 레포 main 의 directory 바로 아래 파일 이름. 디렉터리가 없으면 빈 목록."""
        ...

    async def commit_files(
        self, directory: str, files: Mapping[str, str], message: str, *, protected: Iterable[str] = ()
    ) -> str:
        """gitops 레포의 directory 아래를 files 로 맞추고 커밋한다. 커밋 SHA 를 돌려준다.

        구현체가 지켜야 할 것 세 가지.

        1. directory 안에 있지만 files 에 없는 파일은 지운다. overlay 는 생성물이라
           명세에서 볼륨이 빠졌는데 예전 pvc-*.yaml 이 남으면 kustomize 가 없는
           리소스를 참조한다.
        2. push 가 충돌하면 rebase 해서 다시 시도한다. gitops 에 커밋하는 곳이
           셋이다 — sample-app CI(이미지 태그), review-service CI(이미지 태그),
           그리고 이 노드. 같은 시점에 겹칠 수 있다.
        3. protected(directory 기준 파일 이름 패턴, is_protected) 에 맞는 파일 중 지우게 되는 파일이 있으면 커밋하지 않고
           ProtectedFileRemoval 을 던진다. 충돌로 다시 만들 때마다 새로 읽은 main 기준으로 본다.

        네트워크 오류는 TransientError 로 올린다. 재시도해도 안 되는 충돌도 마찬가지다.
        """
        ...


def commit_message(app: str, env: str, source_commit: str) -> str:
    """gitops 커밋 메시지. CI 의 이미지 태그 갱신과 같은 규칙을 따른다."""
    return f"chore: {app} {env} overlay 갱신 ({source_commit[:7]})"


def _blocked(reason: str) -> dict[str, Any]:
    result: DeployResult = {"status": "blocked", "commit_sha": None, "reason": reason}
    return {"deploy_result": result}


def _committed(sha: str) -> dict[str, Any]:
    result: DeployResult = {"status": "committed", "commit_sha": sha, "reason": None}
    return {"deploy_result": result}


def removed_protected(existing: Iterable[str], rendered_files: Mapping[str, str]) -> list[str]:
    """지금 overlay 에 있는데 렌더 결과에는 없는 보호 파일."""
    return sorted(name for name in set(existing)
                  if name not in rendered_files and is_protected(name, PROTECTED_OVERLAY_FILES))


def removed_reason(removed: list[str]) -> str:
    return f"{OVERLAY_RESOURCE_REMOVED}: {', '.join(removed)}"


async def _render(git: GitClient, state: ReviewState) -> tuple[RenderedOverlay, DeploySpec] | dict[str, Any]:
    """원본 명세 + applied_ops → overlay. 렌더링까지 못 가면 _blocked 결과를 돌려준다."""
    spec_ref = state["spec_ref"]
    repository, source_commit, path = spec_ref["repository"], spec_ref["commit"], spec_ref["path"]

    # ① 수정 지시서. state["patch"] 는 fix 루프를 돌면 비어 있으므로 쓰지 않는다.
    ops = applied_ops(state)

    # ② 원본 명세. Kafka 메시지의 명세는 비밀값이 가려져 있어 렌더러가 거절한다.
    try:
        raw = await git.read_file(repository, path, source_commit)
    except FileNotFoundError:
        return _blocked(f"명세 파일을 찾을 수 없습니다: {repository}@{source_commit[:7]}:{path}")

    # ③ ops 적용 → 명세 검증 → overlay 렌더링
    try:
        spec = DeploySpec.model_validate(apply_ops(yaml.safe_load(raw), ops))
    except ValidationError as e:
        # 검토를 통과한 ops 가 형식에 안 맞는 명세를 만든 경우다. 배포를 멈추고
        # 사유를 돌려준다. 워커가 죽지 않고 대시보드에 이유가 남는 쪽을 택했다.
        return _blocked(f"수정을 적용한 명세가 형식에 맞지 않습니다: {e.error_count()}건")
    return render_overlay(spec), spec


def make_overlay_guard(git: GitClient) -> Callable[[ReviewState], Awaitable[str | None]]:
    """병합 전 검사 — 지금 배포 중인 보호 리소스(ingress 등)를 지우는 명세면 사유, 아니면 None.

    병합 뒤 commit_overlay 에서야 막으면 앱 레포는 병합됐는데 배포는 안 된 상태가 남는다. 그래서 merge_pr 전에 본다.
    렌더링까지 못 가는 명세(파일 없음·형식 오류)는 여기서 막지 않는다 — commit_overlay 가 같은 사유로 blocked 한다.
    """

    async def guard(state: ReviewState) -> str | None:
        out = await _render(git, state)
        if isinstance(out, dict):
            return None
        rendered, _ = out
        removed = removed_protected(await git.list_files(rendered.directory), rendered.files)
        return removed_reason(removed) if removed else None

    return guard


def make_commit_overlay(
    git: GitClient,
) -> Callable[[ReviewState], Awaitable[dict[str, Any]]]:
    """그래프에 끼울 commit_overlay 노드를 만든다. 바뀐 필드(deploy_result)만 돌려준다."""

    async def commit_overlay(state: ReviewState) -> dict[str, Any]:
        out = await _render(git, state)
        if isinstance(out, dict):
            return out
        rendered, spec = out

        # ④ 배포가 깨지거나 명세가 요구한 보호가 빠지는 경고가 있으면 커밋하지 않는다.
        if rendered.blocking:
            reason = " / ".join(f"[{w.code}] {w}" for w in rendered.blocking)
            return _blocked(reason)

        # ⑤ 지금 배포 중인 보호 리소스를 지우는 커밋은 하지 않는다. 병합 전에도 보지만, 그 뒤 gitops 가 바뀌었을 수 있다.
        removed = removed_protected(await git.list_files(rendered.directory), rendered.files)
        if removed:
            return _blocked(removed_reason(removed))

        # 위 검사 뒤 커밋 사이에 gitops 가 바뀌면(충돌 재시도) commit_files 가 새 main 기준으로 다시 본다.
        try:
            sha = await git.commit_files(
                rendered.directory,
                rendered.files,
                commit_message(spec.metadata.name, spec.target.env, state["spec_ref"]["commit"]),
                protected=PROTECTED_OVERLAY_FILES,
            )
        except ProtectedFileRemoval as e:
            return _blocked(removed_reason(e.paths))
        return _committed(sha)

    return commit_overlay
