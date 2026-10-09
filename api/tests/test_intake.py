"""deploy.yaml 없음·빈 파일·형식 오류 PR — 기록, 생성 커밋, PR 커밋 상태, 루프 방지, 다시 처리."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from review_ai.errors import TransientError
from review_ai.judge.fake_llm import ScriptedLLM
from review_ai.secrets_pattern import MASK
from review_ai.spec.deploy_spec import AppSpec
from review_api.app import ApiDeps, create_app, make_repair_llm
from review_api.intake import MAX_READ_BYTES, MAX_SPEC_BYTES, STALE_AFTER, STATUS_CONTEXT, resume_stale_intakes
from review_common.github import FileTooLarge, GitHubError, RefConflict
from tests.test_api import HEAD, REPO, SECRET, FakePublisher, FakeSpecs, pr_event, sample_text, send_pr

NEW = "e" * 40


class FakeGitHub:
    """PR 브랜치 하나(feature)와 커밋 상태 목록. update_branch 는 fast-forward 만 받는다."""

    def __init__(self) -> None:
        self.branch = HEAD
        self.parents: dict[str, str] = {}
        self.contents: dict[str, str] = {}  # 커밋 → deploy.yaml 내용
        self.files: dict[str, dict[str, str | None]] = {}  # 커밋 → 바꾼 파일 전부 (None = 삭제)
        self.statuses: list[dict[str, Any]] = []
        self.status_error: GitHubError | None = None
        self.commit_error: Exception | None = None
        self.tree: dict[str, str] = {}  # PR head 의 파일 — 레포 분석이 읽는다
        self.read_error: GitHubError | None = None
        self.read_limits: dict[str, int | None] = {}

    async def list_files(self, repository: str, ref: str) -> list[str]:
        if self.read_error:
            raise self.read_error
        return list(self.tree)

    async def get_file(self, repository: str, path: str, ref: str, *, max_bytes: int | None = None) -> str:
        self.read_limits[path] = max_bytes
        text = self.tree[path]
        if max_bytes is not None and len(text.encode()) > max_bytes:
            raise FileTooLarge(f"{path} 가 {max_bytes}바이트를 넘는다")
        return text

    async def prepare_files_commit(self, repository: str, *, parent: str, files: dict[str, str | None],
                                   message: str) -> str:
        if self.commit_error:
            raise self.commit_error
        sha = f"{len(self.parents) + 1:x}".rjust(40, "f")
        spec = next(c for p, c in files.items() if p.endswith((".yaml", ".yml")) and not p.startswith(".github/"))
        self.parents[sha], self.contents[sha], self.files[sha] = parent, spec, dict(files)
        self.last_message = message
        return sha

    async def update_branch(self, repository: str, branch: str, sha: str) -> None:
        assert branch == "feature"
        if sha != self.branch and self.parents.get(sha) != self.branch:
            raise RefConflict("non-fast-forward", 422)
        self.branch = sha

    async def create_commit_status(self, repository: str, sha: str, *, state: str, context: str, description: str,
                                   target_url: str | None = None) -> None:
        if self.status_error:
            raise self.status_error
        self.statuses.append({"sha": sha, "state": state, "context": context, "description": description,
                              "target_url": target_url})


class IntakeEnv:
    def __init__(self, *, default_target: str | None = "aws/ap-northeast-2",
                 intake_repositories: frozenset[str] = frozenset({REPO}), repair_llm: Any = None,
                 transform_llm: Any = None, lockfile: Any = None) -> None:
        from review_common.repository import InMemoryReviewRepository

        self.repo = InMemoryReviewRepository()
        self.specs = FakeSpecs()
        self.publisher = FakePublisher()
        self.github = FakeGitHub()
        self.client = TestClient(create_app(ApiDeps(
            repo=self.repo, specs=self.specs, publisher=self.publisher, github_webhook_secret=SECRET,
            github=self.github, default_target=default_target, public_url="https://review.example/",
            intake_repositories=intake_repositories, repair_llm=repair_llm, transform_llm=transform_llm,
            api_token="api-token", **({"lockfile": lockfile} if lockfile else {}))),
            headers={"Authorization": "Bearer api-token"})

    def put(self, text: str, sha: str = HEAD) -> None:
        self.specs.files[(REPO, "deploy.yaml", sha)] = text
        if sha == HEAD:
            self.github.tree["deploy.yaml"] = text  # 복구가 PR head 원문을 읽는다

    async def approve_baseline(self) -> dict[str, Any]:
        spec = yaml.safe_load(sample_text())
        await self.repo.upsert_baseline(app="sample-app", target_env="aws", spec=spec, merge_sha="c0ffee1" + "0" * 33,
                                        spec_ref={"repository": REPO, "commit": "9" * 40, "path": "deploy.yaml"},
                                        observed_at=datetime.now(UTC))
        return spec

    def only_intake(self) -> dict[str, Any]:
        [row] = self.repo.intakes.values()
        return row

    def states(self) -> list[tuple[str, str]]:
        return [(s["sha"], s["state"]) for s in self.github.statuses]


@pytest.fixture
def ienv() -> IntakeEnv:
    return IntakeEnv()


# --- 생성 커밋 → 일반 검토 ---------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "kind"), [(None, "missing"), ("", "empty"), ("# 나중에 채움\n", "empty"),
                                           ("{}", "empty")])
async def test_missing_or_empty_spec_is_regenerated_from_baseline(ienv: IntakeEnv, raw: str | None,
                                                                  kind: str) -> None:
    spec = await ienv.approve_baseline()
    if raw is not None:
        ienv.put(raw)
    resp = send_pr(ienv, pr_event("opened"))

    assert resp.status_code == 202 and resp.json()["kind"] == kind
    row = ienv.only_intake()
    commit = row["result_commit_sha"]
    assert (row["status"], row["reason"], row["kind"], ienv.github.branch) == ("generated", "GENERATED", kind, commit)
    assert ienv.github.parents[commit] == HEAD
    generated = AppSpec.model_validate(yaml.safe_load(ienv.github.contents[commit]))
    assert generated.runtime == AppSpec.model_validate(spec).runtime
    assert ienv.states() == [(HEAD, "pending"), (HEAD, "success")]
    status = ienv.github.statuses[-1]
    assert status["context"] == STATUS_CONTEXT and status["target_url"] == f"https://review.example/intakes/{row['intake_id']}"
    assert ienv.repo.reviews == {} and ienv.publisher.sent == []  # 검토는 생성 커밋의 웹훅에서


async def test_generated_commit_is_reviewed_and_linked(ienv: IntakeEnv) -> None:
    await ienv.approve_baseline()
    send_pr(ienv, pr_event("opened"))
    intake = ienv.only_intake()
    commit = intake["result_commit_sha"]
    ienv.put(ienv.github.contents[commit], sha=commit)

    body = send_pr(ienv, pr_event("synchronize", sha=commit)).json()

    assert body["from_intake"] == intake["intake_id"]
    assert ienv.repo.intakes[intake["intake_id"]]["review_id"] == body["review_id"]
    assert ienv.repo.reviews[body["review_id"]]["pr_head_sha"] == commit
    [(topic, _, _)] = ienv.publisher.sent
    assert topic == "review.requested"


SAMPLE_TREE = {
    "Dockerfile": "FROM node:22-alpine\nEXPOSE 8080\nHEALTHCHECK CMD wget -qO- http://127.0.0.1:8080/healthz\n",
    "package.json": '{"dependencies": {"express": "^4.21.2"}}',
    ".github/workflows/ci.yml": "env:\n  IMAGE: ghcr.io/crystal-sbhackathon2026/sample-app\n"
                                "jobs:\n  b:\n    runs-on: ubuntu-latest\n",
    "src/server.js": "const port = Number(process.env.PORT || 8080);\n",
    "README.md": "# sample\n",
}


async def test_new_app_is_generated_from_repo_analysis(ienv: IntakeEnv) -> None:
    ienv.github.tree = dict(SAMPLE_TREE)
    send_pr(ienv, pr_event("opened"))

    row = ienv.only_intake()
    commit = row["result_commit_sha"]
    assert (row["status"], row["reason"], ienv.github.branch) == ("generated", "GENERATED", commit)
    spec = AppSpec.model_validate(yaml.safe_load(ienv.github.contents[commit]))
    assert (spec.metadata.name, spec.target.env, spec.runtime.port, spec.runtime.health.readiness) == (
        "sample-app", "aws", 8080, "/healthz")
    assert spec.image.repository == "ghcr.io/crystal-sbhackathon2026/sample-app"
    assert "레포 분석" in ienv.github.last_message
    assert "- /runtime: Dockerfile — EXPOSE 8080, HEALTHCHECK /healthz" in ienv.github.last_message.splitlines()


async def test_deleted_spec_before_deploy_report_keeps_committed_spec(ienv: IntakeEnv) -> None:
    """Argo 웹훅 전이라 baselines 가 비어도 gitops 에 커밋한 검토가 있으면 그 명세로 만든다 (sample-app #11).

    레포 분석으로 새로 만들면 ingress·replicas·env 가 빠진 명세가 통과해 overlay 의 ingress 가 지워졌다.
    """
    ienv.github.tree = dict(SAMPLE_TREE)
    spec = yaml.safe_load(sample_text())
    await ienv.repo.insert_review(review_id="r-old", app="sample-app", target_env="aws", repo_id=REPO,
                                  spec_ref={"repository": REPO, "commit": "9" * 40, "path": "deploy.yaml"},
                                  pr_head_sha="9" * 40, requested_by="test")
    await ienv.repo.update_review("r-old", status="committed", final_spec=spec, merge_sha="c0ffee1" + "0" * 33)
    send_pr(ienv, pr_event("opened"))

    row = ienv.only_intake()
    assert (row["status"], row["reason"]) == ("generated", "GENERATED")
    generated = AppSpec.model_validate(yaml.safe_load(ienv.github.contents[row["result_commit_sha"]]))
    previous = AppSpec.model_validate(spec)
    assert (generated.network, generated.runtime) == (previous.network, previous.runtime)
    assert generated.network.ingress is not None and generated.runtime.replicas == 2
    assert row["baseline_used"] is True  # baseline 으로 만들었으니 #41 의 GENERATED_SPEC_UNVERIFIED 대상이 아니다


async def test_large_source_file_is_left_out_and_blocks_absence_claims(ienv: IntakeEnv) -> None:
    ienv.github.tree = {**SAMPLE_TREE, "src/db.js": "// " + "x" * MAX_READ_BYTES + "\nrequire('pg');\n"}
    send_pr(ienv, pr_event("opened"))

    row = ienv.only_intake()
    assert (row["status"], row["reason"], row["result_commit_sha"]) == ("rejected", "UNVERIFIED", None)
    database = next(d for d in row["details"] if d["path"] == "/database")
    assert "큰 파일은 읽지 않는다" in database["message"]  # 못 읽은 파일로 'DB 없음'을 결론내지 않는다
    assert set(ienv.github.read_limits.values()) == {MAX_READ_BYTES}


# --- 배포 대상이 아닌 레포 ---------------------------------------------------------------------------

def test_missing_spec_in_unknown_repo_is_skipped_quietly() -> None:
    """조직 웹훅이라 gitops·인프라 레포 PR 도 온다 — baseline 도 목록도 없는 레포는 예전처럼 skip, 상태 표시도 없다."""
    env = IntakeEnv(intake_repositories=frozenset())
    resp = send_pr(env, pr_event("opened"))

    assert (resp.status_code, resp.json()) == (202, {"skipped": "no deploy.yaml"})
    assert env.repo.intakes == {} and env.github.statuses == [] and env.publisher.sent == []


async def test_missing_spec_in_deployed_repo_opens_intake_without_list() -> None:
    env = IntakeEnv(intake_repositories=frozenset())
    await env.approve_baseline()
    send_pr(env, pr_event("opened"))

    assert env.only_intake()["status"] == "generated"


def test_broken_spec_in_unknown_repo_still_opens_intake() -> None:
    """파일이 있으면 배포하려는 레포다 — 비었거나 깨진 것은 알려 준다."""
    env = IntakeEnv(intake_repositories=frozenset())
    env.put("a: [unclosed")
    send_pr(env, pr_event("opened"))

    assert (env.only_intake()["kind"], env.only_intake()["status"]) == ("yaml_error", "rejected")


# --- 거절 -----------------------------------------------------------------------------------------

async def test_new_app_without_baseline_is_rejected_unverified(ienv: IntakeEnv) -> None:
    send_pr(ienv, pr_event("opened"))

    row = ienv.only_intake()
    assert (row["status"], row["reason"], row["result_commit_sha"]) == ("rejected", "UNVERIFIED", None)
    assert "RUNTIME_UNVERIFIED" in row["message"]
    runtime = next(d for d in row["details"] if d["path"] == "/runtime")
    assert "Dockerfile 이 없어" in runtime["message"]  # 레포 분석이 못 채운 이유
    assert ienv.github.parents == {} and ienv.states() == [(HEAD, "pending"), (HEAD, "failure")]
    verify = ienv.client.get("/verify", params={"sha": HEAD}).json()
    assert (verify["passed"], verify["status"], verify["reasons"], verify["intake_id"]) == (
        False, "intake_failed", ["UNVERIFIED"], row["intake_id"])


def test_no_target_without_baseline_or_default() -> None:
    env = IntakeEnv(default_target=None)
    send_pr(env, pr_event("opened"))

    assert (env.only_intake()["status"], env.only_intake()["reason"]) == ("rejected", "NO_TARGET")


async def test_yaml_error_is_recorded_without_source_text(ienv: IntakeEnv) -> None:
    await ienv.approve_baseline()
    ienv.put("metadata:\n  name: sample-app\nruntime:\n  env:\n    DB_PASSWORD: hunter2\n  port: [8080\n")
    send_pr(ienv, pr_event("opened"))

    row = ienv.only_intake()
    assert (row["kind"], row["status"], row["reason"]) == ("yaml_error", "rejected", "REPAIR_UNAVAILABLE")
    [error] = row["errors"]
    assert error["type"] == "yaml" and error["line"] > 1
    detail = ienv.client.get(f"/intakes/{row['intake_id']}")
    assert detail.status_code == 200 and "hunter2" not in detail.text
    assert ienv.github.parents == {}  # 깨진 명세를 기본값으로 덮지 않는다


async def test_schema_error_keeps_validation_locations(ienv: IntakeEnv) -> None:
    spec = yaml.safe_load(sample_text())
    spec["runtime"]["replica"] = 2
    ienv.put(yaml.safe_dump(spec))
    send_pr(ienv, pr_event("opened"))

    row = ienv.only_intake()
    assert (row["kind"], row["reason"]) == ("schema_error", "REPAIR_UNAVAILABLE")
    assert ["runtime", "replica"] in [e["loc"] for e in row["errors"]]
    assert all("input" not in e for e in row["errors"])


# --- 형식 오류 → LLM 복구 커밋 -----------------------------------------------------------------------

BROKEN = sample_text().replace("  port: 8080", "  port: [8080").replace(
    "DEPLOY_REGION: ap-northeast-2}", "DEPLOY_REGION: ap-northeast-2, DB_PASSWORD: hunter2}")


def repairing_llm(change: dict[str, Any] | None = None) -> ScriptedLLM:
    """형식만 고친 sample 명세(비밀은 가린 채)를 돌려준다. change 가 있으면 runtime 값을 바꿔 쓴다."""

    def produce(request: Any) -> dict[str, Any]:
        assert "hunter2" not in request.user  # 프롬프트에 평문 비밀이 없다
        spec = yaml.safe_load(sample_text())
        spec["runtime"]["env"]["DB_PASSWORD"] = MASK
        spec["runtime"].update(change or {})
        return {"spec_json": json.dumps(spec), "changes": [{"path": "/runtime/port", "why": "닫히지 않은 괄호"}]}

    return ScriptedLLM(produce)


async def test_yaml_error_is_repaired_and_committed() -> None:
    env = IntakeEnv(repair_llm=repairing_llm())
    await env.approve_baseline()
    env.put(BROKEN)
    send_pr(env, pr_event("opened"))

    row = env.only_intake()
    commit = row["result_commit_sha"]
    assert (row["kind"], row["status"], row["reason"], env.github.branch) == ("yaml_error", "repaired", "REPAIRED",
                                                                               commit)
    repaired = AppSpec.model_validate(yaml.safe_load(env.github.contents[commit]))
    assert repaired.runtime.port == 8080 and repaired.runtime.env["DB_PASSWORD"] == "hunter2"
    assert env.github.last_message.startswith("fix: deploy.yaml YAML 문법 오류")
    assert "- /runtime/port: 원문 — 닫히지 않은 괄호" in env.github.last_message
    assert env.states() == [(HEAD, "pending"), (HEAD, "success")]
    assert "hunter2" not in env.client.get(f"/intakes/{row['intake_id']}").text


async def test_repair_that_changes_values_is_rejected() -> None:
    env = IntakeEnv(repair_llm=repairing_llm({"replicas": 5}))
    env.put(BROKEN)
    send_pr(env, pr_event("opened"))

    row = env.only_intake()
    assert (row["status"], row["reason"], row["result_commit_sha"]) == ("rejected", "REPAIR_REJECTED", None)
    conflict = {"code": "VALUE_CONFLICT", "path": "/runtime/replicas", "message": "원문에 있던 값과 다르다"}
    assert conflict in row["details"]
    assert env.github.parents == {} and env.states() == [(HEAD, "pending"), (HEAD, "failure")]


async def test_oversized_broken_spec_is_not_read_into_repair() -> None:
    calls: list[Any] = []
    env = IntakeEnv(repair_llm=ScriptedLLM(lambda request: calls.append(request) or {}))
    await env.approve_baseline()
    env.put(BROKEN + "#" * MAX_SPEC_BYTES)
    send_pr(env, pr_event("opened"))

    row = env.only_intake()
    assert (row["status"], row["reason"], row["result_commit_sha"]) == ("rejected", "REPAIR_REJECTED", None)
    assert [d["code"] for d in row["details"]] == ["TOO_LARGE"]
    assert calls == [] and env.github.read_limits["deploy.yaml"] == MAX_SPEC_BYTES
    assert env.states() == [(HEAD, "pending"), (HEAD, "failure")]


async def test_transient_llm_error_fails_with_retry_hint() -> None:
    class FlakyLLM:
        model = "fake-flaky"

        async def complete(self, request: Any) -> Any:
            raise TransientError("Claude API 529")

    env = IntakeEnv(repair_llm=FlakyLLM())
    env.put(BROKEN)
    send_pr(env, pr_event("opened"))

    row = env.only_intake()
    assert (row["status"], row["reason"]) == ("failed", "ERROR") and "다시 처리한다" in row["message"]
    assert env.states() == [(HEAD, "pending"), (HEAD, "error")]


def test_repair_llm_finishes_before_stale_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = make_repair_llm()._inner._client  # type: ignore[union-attr]

    assert client.timeout * (client.max_retries + 1) < STALE_AFTER.total_seconds()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert make_repair_llm() is None


async def test_fork_pr_is_not_committed(ienv: IntakeEnv) -> None:
    """포크 PR 은 웹훅에서 바로 건너뛴다 — intake 도 열지 않는다 (예전엔 intake 를 열고 FORK_PR 로 거절했다)."""
    await ienv.approve_baseline()
    resp = send_pr(ienv, pr_event("opened", head_repo="someone/sample-app"))

    assert resp.json() == {"skipped": "fork"}
    assert ienv.repo.intakes == {} and ienv.github.parents == {}


async def test_intake_commit_needing_intake_again_stops(ienv: IntakeEnv) -> None:
    """생성 커밋인데도 명세가 없다고 나오면 다시 만들지 않는다 — 웹훅·커밋이 끝없이 반복되지 않게."""
    await ienv.approve_baseline()
    send_pr(ienv, pr_event("opened"))
    commit = ienv.only_intake()["result_commit_sha"]

    send_pr(ienv, pr_event("synchronize", sha=commit))  # 생성 파일을 못 읽었다고 가정

    row = await ienv.repo.find_intake_by_head(REPO, commit)
    assert (row["status"], row["reason"]) == ("rejected", "LOOP_GUARD")
    assert len(ienv.github.parents) == 1 and ienv.github.branch == commit


async def test_branch_moved_meanwhile_is_not_forced(ienv: IntakeEnv) -> None:
    await ienv.approve_baseline()
    ienv.github.branch = NEW  # 웹훅과 처리 사이에 개발자가 새 커밋을 올렸다
    send_pr(ienv, pr_event("opened"))

    row = ienv.only_intake()
    assert (row["status"], row["reason"]) == ("rejected", "BRANCH_MOVED")
    assert ienv.github.branch == NEW


# --- 중복·오류·다시 처리 -------------------------------------------------------------------------------

async def test_new_commit_without_spec_supersedes_open_reviews(ienv: IntakeEnv) -> None:
    ienv.put(sample_text())
    old = send_pr(ienv, pr_event("opened")).json()["review_id"]
    await ienv.repo.update_review(old, status="needs_human")

    body = send_pr(ienv, pr_event("synchronize", sha=NEW)).json()  # 새 커밋에서 deploy.yaml 을 지웠다

    assert body["superseded"] == [old]
    assert (ienv.repo.reviews[old]["status"], ienv.repo.reviews[old]["superseded_by"]) == ("superseded", None)


async def test_same_sha_twice_makes_one_intake(ienv: IntakeEnv) -> None:
    await ienv.approve_baseline()
    first = send_pr(ienv, pr_event("opened")).json()
    second = send_pr(ienv, pr_event("reopened")).json()

    assert second == {"skipped": "already taken", "intake_id": first["intake_id"]}
    assert len(ienv.repo.intakes) == 1 and len(ienv.github.parents) == 1


async def test_status_permission_error_keeps_the_record(ienv: IntakeEnv) -> None:
    await ienv.approve_baseline()
    ienv.github.status_error = GitHubError("커밋 상태 기록 실패 403", 403)
    send_pr(ienv, pr_event("opened"))

    assert ienv.only_intake()["status"] == "generated" and ienv.github.statuses == []


async def test_repo_read_failure_marks_failed(ienv: IntakeEnv) -> None:
    ienv.github.read_error = GitHubError("트리 조회 실패 503", 503)
    send_pr(ienv, pr_event("opened"))

    assert (ienv.only_intake()["status"], ienv.only_intake()["reason"]) == ("failed", "ERROR")


async def test_github_failure_marks_failed_with_error_status(ienv: IntakeEnv) -> None:
    await ienv.approve_baseline()
    ienv.github.commit_error = GitHubError("트리 생성 실패 502", 502)
    send_pr(ienv, pr_event("opened"))

    row = ienv.only_intake()
    assert (row["status"], row["reason"]) == ("failed", "ERROR")
    assert "새 커밋을 올리면 다시 처리한다" in row["message"]
    assert ienv.states() == [(HEAD, "pending"), (HEAD, "error")]


async def test_stale_processing_row_is_resumed_with_its_commit(ienv: IntakeEnv) -> None:
    """커밋을 만들고 브랜치를 옮기기 전에 파드가 죽었다 → 같은 커밋으로 마저 끝낸다."""
    await ienv.approve_baseline()
    await ienv.repo.insert_intake(intake_id="in_x", repository=REPO, head_repository=REPO, pr_number=5,
                                  head_sha=HEAD, head_ref="feature", path="deploy.yaml", kind="missing",
                                  errors=[], requested_by="octo-dev")
    made = await ienv.github.prepare_files_commit(REPO, parent=HEAD, files={"deploy.yaml": "x"}, message="m")
    await ienv.repo.link_intake("in_x", result_commit_sha=made)
    deps = ienv.client.app.state.deps
    assert await resume_stale_intakes(deps) == []  # 아직 다른 파드가 처리 중일 수 있다

    ienv.repo.intakes["in_x"]["updated_at"] -= STALE_AFTER + timedelta(seconds=1)
    assert await resume_stale_intakes(deps) == ["in_x"]

    row = ienv.repo.intakes["in_x"]
    assert (row["status"], row["result_commit_sha"], ienv.github.branch) == ("generated", made, made)
    assert len(ienv.github.parents) == 1


async def test_finished_intake_is_not_processed_twice(ienv: IntakeEnv) -> None:
    from review_api.intake import process_intake

    await ienv.approve_baseline()
    send_pr(ienv, pr_event("opened"))
    row = ienv.only_intake()
    await process_intake(ienv.client.app.state.deps, row["intake_id"])

    assert len(ienv.github.parents) == 1 and len(ienv.github.statuses) == 2


async def _insert_new_app_intake(ienv: IntakeEnv, intake_id: str) -> None:
    ienv.github.tree = dict(SAMPLE_TREE)  # baseline 없는 새 앱 → 레포 분석(GitHub 읽기 수십 번)
    await ienv.repo.insert_intake(intake_id=intake_id, repository=REPO, head_repository=REPO, pr_number=5,
                                  head_sha=HEAD, head_ref="feature", path="deploy.yaml", kind="missing",
                                  errors=[], requested_by="octo-dev")


async def test_intake_swept_while_still_processing_commits_once(ienv: IntakeEnv) -> None:
    """처리가 STALE_AFTER 를 넘기는 사이 다른 파드의 sweep 이 같은 행을 끝냈다 → 늦은 쪽은 커밋하지 않는다."""
    from review_api import intake as intake_mod

    await _insert_new_app_intake(ienv, "in_slow")
    deps = ienv.client.app.state.deps
    real_list = ienv.github.list_files
    swept: list[list[str]] = []

    async def slow_list(repository: str, ref: str) -> list[str]:
        if not swept:  # 첫 처리가 GitHub 읽기에서 멈춘 동안 sweep 이 돈다
            swept.append([])
            ienv.repo.intakes["in_slow"]["updated_at"] -= STALE_AFTER + timedelta(seconds=1)
            swept[0] = await intake_mod.resume_stale_intakes(deps)
        return await real_list(repository, ref)

    ienv.github.list_files = slow_list  # type: ignore[method-assign]
    await intake_mod.process_intake(deps, "in_slow")

    row = ienv.repo.intakes["in_slow"]
    assert swept == [["in_slow"]]
    assert (row["status"], len(ienv.github.parents)) == ("generated", 1)
    assert row["result_commit_sha"] == ienv.github.branch  # 생성 커밋 웹훅·LOOP_GUARD 가 이 SHA 로 행을 찾는다
    assert ienv.states() == [(HEAD, "pending"), (HEAD, "pending"), (HEAD, "success")]


async def test_link_intake_keeps_the_first_commit(ienv: IntakeEnv) -> None:
    """두 곳이 동시에 커밋을 만들어도 행에는 먼저 이은 커밋이 남고, 둘 다 그 커밋으로 브랜치를 옮긴다."""
    await _insert_new_app_intake(ienv, "in_x")
    first, late = "a" * 40, "b" * 40

    assert await ienv.repo.link_intake("in_x", result_commit_sha=first) == first
    assert await ienv.repo.link_intake("in_x", result_commit_sha=late) == first
    assert await ienv.repo.link_intake("in_x", review_id="rv_1") == first
    assert ienv.repo.intakes["in_x"]["result_commit_sha"] == first


async def test_processing_intake_heartbeat_keeps_sweep_away(ienv: IntakeEnv,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """처리가 길어져도 살아 있는 동안 updated_at 을 갱신한다 → sweep 은 죽은 파드의 행만 가져간다."""
    import asyncio

    from review_api import intake as intake_mod

    monkeypatch.setattr(intake_mod, "HEARTBEAT_EVERY", timedelta(milliseconds=5))
    await _insert_new_app_intake(ienv, "in_slow")
    deps = ienv.client.app.state.deps
    real_list = ienv.github.list_files
    claimed: list[list[dict[str, Any]]] = []

    async def slow_list(repository: str, ref: str) -> list[str]:
        ienv.repo.intakes["in_slow"]["updated_at"] -= STALE_AFTER + timedelta(seconds=1)
        await asyncio.sleep(0.05)  # GitHub 읽기가 STALE_AFTER 를 넘긴 셈
        claimed.append(await ienv.repo.claim_stale_intakes(STALE_AFTER))
        return await real_list(repository, ref)

    ienv.github.list_files = slow_list  # type: ignore[method-assign]
    await intake_mod.process_intake(deps, "in_slow")

    row = ienv.repo.intakes["in_slow"]
    assert claimed == [[]]
    assert (row["status"], row["result_commit_sha"], len(ienv.github.parents)) == (
        "generated", ienv.github.branch, 1)
    await asyncio.sleep(0)
    assert asyncio.all_tasks() == {asyncio.current_task()}  # 끝나면 heartbeat 도 멈춘다


# --- 조회·직접 요청 --------------------------------------------------------------------------------

def test_unknown_intake_is_404(ienv: IntakeEnv) -> None:
    assert ienv.client.get("/intakes/in_nope").status_code == 404


def test_without_github_writer_records_and_rejects(ienv: IntakeEnv) -> None:
    ienv.client.app.state.deps.github = None
    send_pr(ienv, pr_event("opened"))
    row = ienv.only_intake()

    assert (row["status"], row["reason"]) == ("rejected", "COMMIT_UNAVAILABLE")
    detail = ienv.client.get(f"/intakes/{row['intake_id']}").json()
    assert (detail["kind"], detail["pr_number"], detail["head_sha"]) == ("missing", 5, HEAD)


@pytest.mark.parametrize(("raw", "message"), [("", "deploy.yaml 가 비었다"), ("# 주석뿐\n", "deploy.yaml 가 비었다"),
                                              ("a: [unclosed", "deploy.yaml YAML 파싱 실패")])
def test_direct_request_keeps_422_for_empty_or_broken(ienv: IntakeEnv, raw: str, message: str) -> None:
    ienv.put(raw)
    resp = ienv.client.post("/reviews", json={"spec_ref": {"repository": REPO, "commit": HEAD},
                                              "requested_by": "hyeyeon"})

    assert resp.status_code == 422 and resp.json()["detail"]["message"] == message
    assert ienv.repo.intakes == {} and ienv.repo.reviews == {}  # 직접 요청은 intake 를 만들지 않는다


async def test_github_client_posts_commit_status() -> None:
    import httpx

    from review_common.github import API, GitHubClient

    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201 if len(seen) == 1 else 403, json={})

    client = GitHubClient("t", client=httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handler)))
    await client.create_commit_status(REPO, HEAD, state="failure", context=STATUS_CONTEXT, description="x" * 200,
                                      target_url="https://review.example/intakes/in_1")
    with pytest.raises(GitHubError) as denied:
        await client.create_commit_status(REPO, HEAD, state="pending", context=STATUS_CONTEXT, description="y")

    path, body = seen[0]
    assert path == f"/repos/{REPO}/statuses/{HEAD}"
    assert (body["state"], len(body["description"]), body["target_url"]) == (
        "failure", 140, "https://review.example/intakes/in_1")
    assert "target_url" not in seen[1][1] and denied.value.status_code == 403


async def test_github_client_get_file_stops_at_max_bytes() -> None:
    import httpx

    from review_common.github import API, GitHubClient, SpecNotFound

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/missing.yaml"):
            return httpx.Response(404)
        return httpx.Response(200, content="한글 deploy".encode())

    client = GitHubClient("t", client=httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handler)))

    assert await client.get_file(REPO, "deploy.yaml", HEAD) == "한글 deploy"
    assert await client.get_file(REPO, "deploy.yaml", HEAD, max_bytes=13) == "한글 deploy"
    with pytest.raises(FileTooLarge) as too_large:
        await client.get_file(REPO, "deploy.yaml", HEAD, max_bytes=12)
    assert too_large.value.status_code == 413 and not too_large.value.transient
    with pytest.raises(SpecNotFound):
        await client.get_file(REPO, "missing.yaml", HEAD)


# --- 커밋 상태 review-service/verify (⑤) ----------------------------------------------------------------

def _verify_statuses(ienv: IntakeEnv) -> list[dict[str, Any]]:
    return [s for s in ienv.github.statuses if s["context"] == "review-service/verify"]


async def test_new_review_writes_pending_with_link(ienv: IntakeEnv) -> None:
    ienv.put(sample_text())
    rid = send_pr(ienv, pr_event("opened")).json()["review_id"]

    [status] = _verify_statuses(ienv)
    assert (status["sha"], status["state"], status["description"]) == (HEAD, "pending", "AI 검토 중")
    assert status["target_url"] == f"https://review.example/ui/reviews/{rid}"


async def test_status_error_does_not_block_review(ienv: IntakeEnv) -> None:
    ienv.github.status_error = GitHubError("커밋 상태 기록 실패 403", 403)
    ienv.put(sample_text())
    resp = send_pr(ienv, pr_event("opened"))

    assert "review_id" in resp.json()
    assert ienv.repo.reviews[resp.json()["review_id"]]["status"] == "received"
