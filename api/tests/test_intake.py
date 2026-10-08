"""deploy.yaml 없음·빈 파일·형식 오류 PR — 기록, 생성 커밋, PR 커밋 상태, 루프 방지, 다시 처리."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from review_ai.spec.deploy_spec import AppSpec
from review_api.app import ApiDeps, create_app
from review_api.intake import STALE_AFTER, STATUS_CONTEXT, resume_stale_intakes
from review_common.github import GitHubError, RefConflict
from tests.test_api import HEAD, REPO, SECRET, FakePublisher, FakeSpecs, pr_event, sample_text, send_pr

NEW = "e" * 40


class FakeGitHub:
    """PR 브랜치 하나(feature)와 커밋 상태 목록. update_branch 는 fast-forward 만 받는다."""

    def __init__(self) -> None:
        self.branch = HEAD
        self.parents: dict[str, str] = {}
        self.contents: dict[str, str] = {}
        self.statuses: list[dict[str, Any]] = []
        self.status_error: GitHubError | None = None
        self.commit_error: Exception | None = None

    async def prepare_file_commit(self, repository: str, *, parent: str, path: str, content: str,
                                  message: str) -> str:
        if self.commit_error:
            raise self.commit_error
        sha = f"{len(self.parents) + 1:x}".rjust(40, "f")
        self.parents[sha], self.contents[sha] = parent, content
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
                 intake_repositories: frozenset[str] = frozenset({REPO})) -> None:
        from review_common.repository import InMemoryReviewRepository

        self.repo = InMemoryReviewRepository()
        self.specs = FakeSpecs()
        self.publisher = FakePublisher()
        self.github = FakeGitHub()
        self.client = TestClient(create_app(ApiDeps(
            repo=self.repo, specs=self.specs, publisher=self.publisher, github_webhook_secret=SECRET,
            github=self.github, default_target=default_target, public_url="https://review.example/",
            intake_repositories=intake_repositories)))

    def put(self, text: str, sha: str = HEAD) -> None:
        self.specs.files[(REPO, "deploy.yaml", sha)] = text

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
    assert "RUNTIME_UNVERIFIED" in row["message"] and row["details"]
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


async def test_fork_pr_is_not_committed(ienv: IntakeEnv) -> None:
    await ienv.approve_baseline()
    send_pr(ienv, pr_event("opened", head_repo="someone/sample-app"))

    assert (ienv.only_intake()["status"], ienv.only_intake()["reason"]) == ("rejected", "FORK_PR")
    assert ienv.github.parents == {}


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
    made = await ienv.github.prepare_file_commit(REPO, parent=HEAD, path="deploy.yaml", content="x", message="m")
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
