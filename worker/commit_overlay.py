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

from typing import Any, Awaitable, Callable, Mapping, Protocol

import yaml
from pydantic import ValidationError

from review_ai.overlay import render_overlay
from review_ai.patching import apply_ops
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.state import DeployResult, ReviewState
from review_ai.verdict import applied_ops

__all__ = ["GitClient", "make_commit_overlay", "commit_message"]


class GitClient(Protocol):
    """커밋 노드가 쓰는 Git 접근. 네트워크 오류는 구현체가 TransientError 로 올린다."""

    async def read_file(self, repository: str, path: str, ref: str) -> str:
        """앱 레포의 특정 커밋에서 파일 내용을 읽는다. 없으면 FileNotFoundError."""
        ...

    async def commit_files(
        self, directory: str, files: Mapping[str, str], message: str
    ) -> str:
        """gitops 레포의 directory 아래를 files 로 맞추고 커밋한다. 커밋 SHA 를 돌려준다.

        directory 안에 있지만 files 에 없는 파일은 지운다. overlay 는 생성물이라
        남은 파일이 있으면 kustomize 가 없는 리소스를 참조할 수 있다.
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


def make_commit_overlay(
    git: GitClient,
) -> Callable[[ReviewState], Awaitable[dict[str, Any]]]:
    """그래프에 끼울 commit_overlay 노드를 만든다. 바뀐 필드(deploy_result)만 돌려준다."""

    async def commit_overlay(state: ReviewState) -> dict[str, Any]:
        spec_ref = state["spec_ref"]
        repository, source_commit, path = (
            spec_ref["repository"],
            spec_ref["commit"],
            spec_ref["path"],
        )

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

        rendered = render_overlay(spec)

        # ④ 배포가 깨지거나 명세가 요구한 보호가 빠지는 경고가 있으면 커밋하지 않는다.
        if rendered.blocking:
            reason = " / ".join(f"[{w.code}] {w}" for w in rendered.blocking)
            return _blocked(reason)

        sha = await git.commit_files(
            rendered.directory,
            rendered.files,
            commit_message(spec.metadata.name, spec.target.env, source_commit),
        )
        return _committed(sha)

    return commit_overlay
