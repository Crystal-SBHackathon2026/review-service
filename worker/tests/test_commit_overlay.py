"""commit_overlay 노드 테스트. 네트워크 없이 가짜 Git 클라이언트로 돈다."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pytest
import yaml

from review_ai.errors import TransientError
from review_common.github import GitHubGitClient
from review_worker.commit_overlay import (PROTECTED_OVERLAY_FILES, commit_message, make_commit_overlay,
                                          make_overlay_guard)
from tests.test_git_client import FakeGitHubApi, client, files_at_head

SAMPLES = Path(__file__).resolve().parents[2] / "ai" / "samples"
REPO = "Crystal-SBHackathon2026/sample-app"
COMMIT = "b084c24e5a45f307981a9a005fa9d4e1e1079062"


class FakeGit:
    """읽기는 샘플 파일에서, 커밋은 메모리에 기록한다."""

    def __init__(self, sample: str, *, read_error: Exception | None = None,
                 commit_error: Exception | None = None) -> None:
        self.raw = (SAMPLES / sample).read_text(encoding="utf-8")
        self.read_error = read_error
        self.commit_error = commit_error
        self.commits: list[tuple[str, dict[str, str], str]] = []
        self.reads: list[tuple[str, str, str]] = []
        self.existing: list[str] = []  # gitops 에 지금 있는 overlay 파일 (기본: 새 앱)

    async def list_files(self, directory: str) -> list[str]:
        return list(self.existing)

    async def read_file(self, repository: str, path: str, ref: str) -> str:
        self.reads.append((repository, path, ref))
        if self.read_error:
            raise self.read_error
        return self.raw

    async def commit_files(self, directory: str, files: Mapping[str, str], message: str, *,
                           protected=()) -> str:
        self.protected = tuple(protected)
        if self.commit_error:
            raise self.commit_error
        self.commits.append((directory, dict(files), message))
        return "a" * 40


def state(sample_env: str = "aws", *, rounds=None, patch=None, verdict="pass"):
    return {
        "review_id": "rv_20261008_0001",
        "target_env": sample_env,
        "spec_ref": {"repository": REPO, "commit": COMMIT, "path": "deploy.yaml"},
        "decision": {"verdict": verdict},
        "rounds": rounds or [],
        "patch": patch,
    }


@pytest.mark.asyncio
async def test_수정_없이_통과하면_원본_그대로_커밋한다():
    git = FakeGit("01-pass-sample-app-aws.yaml")
    out = await make_commit_overlay(git)(state())

    assert out == {"deploy_result": {"status": "committed", "commit_sha": "a" * 40, "reason": None}}
    assert len(out) == 1, "바뀐 필드만 돌려줘야 한다"
    assert git.reads == [(REPO, "deploy.yaml", COMMIT)]

    directory, files, message = git.commits[0]
    assert directory == "apps/sample-app/overlays/aws"
    assert "kustomization.yaml" in files
    assert message == "chore: sample-app aws overlay 갱신 (b084c24)"


@pytest.mark.asyncio
async def test_overlay_에_이미지가_들어가지_않는다():
    """이미지 태그는 CI 가 base 의 newTag 로 정한다. overlay 가 덮으면 갱신이 무시된다."""
    git = FakeGit("01-pass-sample-app-aws.yaml")
    await make_commit_overlay(git)(state())

    kustomization = yaml.safe_load(git.commits[0][1]["kustomization.yaml"])
    assert "images" not in kustomization


@pytest.mark.asyncio
async def test_고쳐서_통과하면_수정이_반영된다():
    """fix 루프를 돈 최종 State 는 patch 가 비어 있다. rounds 의 ops 를 써야 한다."""
    git = FakeGit("01-pass-sample-app-aws.yaml")
    ops = [{"op": "replace", "path": "/runtime/replicas", "value": 1}]
    await make_commit_overlay(git)(state(rounds=[{"patch": {"ops": ops}}]))

    kustomization = yaml.safe_load(git.commits[0][1]["kustomization.yaml"])
    patch = yaml.safe_load(kustomization["patches"][0]["patch"])
    replicas = [o for o in patch if o["path"] == "/spec/replicas"]
    assert replicas and replicas[0]["value"] == 1, "수정한 복제 수가 반영돼야 한다"


@pytest.mark.asyncio
async def test_입력_State_를_바꾸지_않는다():
    git = FakeGit("01-pass-sample-app-aws.yaml")
    ops = [{"op": "replace", "path": "/runtime/replicas", "value": 1}]
    s = state(rounds=[{"patch": {"ops": ops}}])
    before = yaml.safe_dump(s, sort_keys=True)

    await make_commit_overlay(git)(s)
    assert yaml.safe_dump(s, sort_keys=True) == before


@pytest.mark.asyncio
async def test_blocking_경고가_있으면_커밋하지_않고_사유를_돌려준다():
    """로컬에 대역 제한을 요구하는 명세 — 강제할 수 없어 중단 대상이다."""
    git = FakeGit("05-fix-engine-unsupported-local.yaml")
    out = await make_commit_overlay(git)(state("local"))

    result = out["deploy_result"]
    assert result["status"] == "blocked"
    assert result["commit_sha"] is None
    assert result["reason"], "왜 멈췄는지 들어 있어야 한다"
    assert git.commits == [], "중단했으면 커밋하지 않는다"


@pytest.mark.asyncio
async def test_명세_파일이_없으면_blocked():
    git = FakeGit("01-pass-sample-app-aws.yaml", read_error=FileNotFoundError())
    out = await make_commit_overlay(git)(state())

    assert out["deploy_result"]["status"] == "blocked"
    assert "deploy.yaml" in out["deploy_result"]["reason"]


@pytest.mark.asyncio
async def test_일시적_오류는_그대로_올린다():
    """재시도하면 되는 오류는 blocked 로 삼키지 않는다. RetryPolicy 가 처리한다."""
    git = FakeGit("01-pass-sample-app-aws.yaml", commit_error=TransientError("push 실패"))
    with pytest.raises(TransientError):
        await make_commit_overlay(git)(state())


@pytest.mark.asyncio
async def test_수정이_명세를_깨뜨리면_blocked():
    git = FakeGit("01-pass-sample-app-aws.yaml")
    ops = [{"op": "replace", "path": "/runtime/port", "value": "포트가_아님"}]
    out = await make_commit_overlay(git)(state(rounds=[{"patch": {"ops": ops}}]))

    assert out["deploy_result"]["status"] == "blocked"
    assert "형식" in out["deploy_result"]["reason"]


def test_커밋_메시지_형식():
    assert commit_message("sample-app", "aws", "b084c24e5a45f3") == \
        "chore: sample-app aws overlay 갱신 (b084c24)"


@pytest.mark.asyncio
async def test_지금_있는_ingress_를_지우는_커밋은_막는다():
    git = FakeGit("01-pass-sample-app-aws.yaml")
    git.raw = git.raw.replace("network:\n  ingress: {public: true, tls: false}\n", "")
    git.existing = ["ingress.yaml", "kustomization.yaml"]
    out = await make_commit_overlay(git)(state())

    assert out == {"deploy_result": {"status": "blocked", "commit_sha": None,
                                     "reason": "OVERLAY_RESOURCE_REMOVED: ingress.yaml"}}
    assert git.commits == []


@pytest.mark.asyncio
async def test_병합_전_검사는_ingress_삭제만_사유로_돌려준다():
    from review_worker.commit_overlay import make_overlay_guard

    keep = FakeGit("01-pass-sample-app-aws.yaml")
    keep.existing = ["ingress.yaml", "kustomization.yaml"]
    drop = FakeGit("01-pass-sample-app-aws.yaml")
    drop.raw = drop.raw.replace("network:\n  ingress: {public: true, tls: false}\n", "")
    drop.existing = ["ingress.yaml"]

    assert await make_overlay_guard(keep)(state()) is None
    assert await make_overlay_guard(drop)(state()) == "OVERLAY_RESOURCE_REMOVED: ingress.yaml"


# --- 커밋 재시도 중에 생긴 보호 파일 (실제 GitHubGitClient + 가짜 GitHub API) -----------------------------

class SampleGitClient(GitHubGitClient):
    """gitops 쪽은 실제 GitHubGitClient, 앱 레포 명세는 샘플."""

    raw = ""

    async def read_file(self, repository: str, path: str, ref: str) -> str:
        return self.raw


def gitops_client(api: FakeGitHubApi, raw: str) -> SampleGitClient:
    git = client(api, SampleGitClient)
    git.raw = raw
    return git


NO_INGRESS = "network:\n  ingress: {public: true, tls: false}\n"
OVERLAY_DIR = "apps/sample-app/overlays/aws"


@pytest.mark.asyncio
async def test_재시도_중에_ingress_가_생기면_지우지_않고_blocked():
    raw = FakeGit("01-pass-sample-app-aws.yaml").raw.replace(NO_INGRESS, "")
    api = FakeGitHubApi({f"{OVERLAY_DIR}/kustomization.yaml": "old"})  # 첫 검사 때는 ingress 없음
    api.conflicts = 1  # 그 사이 다른 곳이 ingress.yaml 을 넣었다
    api.conflict_files = {f"{OVERLAY_DIR}/kustomization.yaml": "old", f"{OVERLAY_DIR}/ingress.yaml": "ingress"}

    out = await make_commit_overlay(gitops_client(api, raw))(state())

    assert out == {"deploy_result": {"status": "blocked", "commit_sha": None,
                                     "reason": "OVERLAY_RESOURCE_REMOVED: ingress.yaml"}}
    assert (len(api.created_trees), api.created_commits, api.ref_updates) == (1, 1, 1)
    assert f"{OVERLAY_DIR}/ingress.yaml" in files_at_head(api)


@pytest.mark.asyncio
async def test_명세에_ingress_가_있으면_재시도해도_정상_커밋():
    raw = FakeGit("01-pass-sample-app-aws.yaml").raw
    api = FakeGitHubApi({f"{OVERLAY_DIR}/kustomization.yaml": "old"})
    api.conflicts = 1
    api.conflict_files = {f"{OVERLAY_DIR}/kustomization.yaml": "old", f"{OVERLAY_DIR}/ingress.yaml": "ingress"}

    out = await make_commit_overlay(gitops_client(api, raw))(state())

    assert out["deploy_result"]["status"] == "committed" and out["deploy_result"]["commit_sha"] == api.head
    assert files_at_head(api)[f"{OVERLAY_DIR}/ingress.yaml"] != "ingress"


@pytest.mark.asyncio
async def test_커밋할_때_보호_파일_목록을_넘긴다():
    git = FakeGit("01-pass-sample-app-aws.yaml")
    await make_commit_overlay(git)(state())
    assert git.protected == PROTECTED_OVERLAY_FILES


# --- PVC: 볼륨을 빼는 명세가 PVC(와 local-path 데이터)를 지우지 않게 -----------------------------------

SQLITE_SAMPLE = "02-pass-local-sqlite.yaml"
LOCAL_DIR = "apps/todo/overlays/local"


def without_sqlite_volume(raw: str) -> str:
    """SQLite 를 버리고 영속 볼륨을 뺀 명세 — 렌더 결과에 pvc-data.yaml 이 없다."""
    spec = yaml.safe_load(raw)
    for key in ("database", "storage"):
        spec.pop(key)
    spec["requirements"] = {"persistence": False}
    return yaml.safe_dump(spec, sort_keys=False)


@pytest.mark.asyncio
async def test_지금_있는_pvc_를_지우는_커밋은_막는다():
    git = FakeGit(SQLITE_SAMPLE)
    git.raw = without_sqlite_volume(git.raw)
    git.existing = ["deployment-patch.yaml", "kustomization.yaml", "pvc-data.yaml"]
    out = await make_commit_overlay(git)(state("local"))

    assert out == {"deploy_result": {"status": "blocked", "commit_sha": None,
                                     "reason": "OVERLAY_RESOURCE_REMOVED: pvc-data.yaml"}}
    assert git.commits == []


@pytest.mark.asyncio
async def test_볼륨_이름을_바꿔도_예전_pvc_삭제로_막는다():
    git = FakeGit(SQLITE_SAMPLE)
    git.raw = git.raw.replace("volume: data,", "volume: db,").replace("{name: data,", "{name: db,")
    git.existing = ["kustomization.yaml", "pvc-data.yaml"]

    assert await make_overlay_guard(git)(state("local")) == "OVERLAY_RESOURCE_REMOVED: pvc-data.yaml"


@pytest.mark.asyncio
async def test_볼륨을_유지하면_pvc_가_있어도_통과한다():
    git = FakeGit(SQLITE_SAMPLE)
    git.existing = ["kustomization.yaml", "pvc-data.yaml"]

    assert await make_overlay_guard(git)(state("local")) is None
    out = await make_commit_overlay(git)(state("local"))
    assert out["deploy_result"]["status"] == "committed"
    assert "pvc-data.yaml" in git.commits[0][1]


@pytest.mark.asyncio
async def test_병합_전_검사도_pvc_삭제를_사유로_돌려준다():
    git = FakeGit(SQLITE_SAMPLE)
    git.raw = without_sqlite_volume(git.raw)
    git.existing = ["kustomization.yaml", "pvc-data.yaml"]

    assert await make_overlay_guard(git)(state("local")) == "OVERLAY_RESOURCE_REMOVED: pvc-data.yaml"


@pytest.mark.asyncio
async def test_재시도_중에_pvc_가_생기면_지우지_않고_blocked():
    raw = without_sqlite_volume(FakeGit(SQLITE_SAMPLE).raw)
    api = FakeGitHubApi({f"{LOCAL_DIR}/kustomization.yaml": "old"})  # 첫 검사 때는 PVC 없음
    api.conflicts = 1  # 그 사이 다른 커밋이 pvc-data.yaml 을 넣었다
    api.conflict_files = {f"{LOCAL_DIR}/kustomization.yaml": "old", f"{LOCAL_DIR}/pvc-data.yaml": "pvc"}

    out = await make_commit_overlay(gitops_client(api, raw))(state("local"))

    assert out == {"deploy_result": {"status": "blocked", "commit_sha": None,
                                     "reason": "OVERLAY_RESOURCE_REMOVED: pvc-data.yaml"}}
    assert files_at_head(api)[f"{LOCAL_DIR}/pvc-data.yaml"] == "pvc"
