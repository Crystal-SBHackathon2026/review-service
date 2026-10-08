"""Review API — 샘플 01 은 202, 형식 오류 명세는 422. 나머지 엔드포인트와 웹훅."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from review_ai.messages import ReviewRequested
from review_api.app import ApiDeps, create_app
from review_common.ids import new_review_id
from review_common.github import SpecNotFound
from review_common.repository import InMemoryReviewRepository
from review_common.resumed import parse_review_resumed

SAMPLES = Path(__file__).resolve().parents[2] / "ai" / "samples"
REPO = "Crystal-SBHackathon2026/sample-app"
HEAD = "a" * 40
SECRET = "webhook-secret"


class FakeSpecs:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str, str], str] = {}

    async def get_file(self, repository: str, path: str, ref: str) -> str:
        try:
            return self.files[(repository, path, ref)]
        except KeyError:
            raise SpecNotFound(f"{repository}@{ref} 에 {path} 가 없다", 404) from None


class FakePublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bytes]] = []

    async def send(self, topic: str, key: str, value: bytes) -> None:
        self.sent.append((topic, key, value))


class Env:
    def __init__(self) -> None:
        self.repo = InMemoryReviewRepository()
        self.specs = FakeSpecs()
        self.publisher = FakePublisher()
        self.app = create_app(ApiDeps(repo=self.repo, specs=self.specs, publisher=self.publisher,
                                      github_webhook_secret=SECRET, argocd_webhook_token="argo-token"))
        self.client = TestClient(self.app)

    def put_spec(self, text: str, sha: str = HEAD) -> None:
        self.specs.files[(REPO, "deploy.yaml", sha)] = text

    def request_review(self, sha: str = HEAD) -> Any:
        body = {"spec_ref": {"repository": REPO, "commit": sha, "path": "deploy.yaml"}, "requested_by": "hyeyeon"}
        return self.client.post("/reviews", json=body)

    async def set_status(self, review_id: str, **fields: Any) -> None:
        await self.repo.update_review(review_id, **fields)


@pytest.fixture
def env() -> Env:
    return Env()


def sample_text(name: str = "01-pass-sample-app-aws.yaml") -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def signed(body: dict[str, Any]) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    return raw, "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


# --- POST /reviews --------------------------------------------------------------------------------

def test_healthz(env: Env) -> None:
    assert env.client.get("/healthz").status_code == 200


def test_sample_01_accepted_and_published(env: Env) -> None:
    env.put_spec(sample_text())
    resp = env.request_review()

    assert resp.status_code == 202
    review_id = resp.json()["review_id"]
    assert review_id.startswith("rv_")
    row = env.repo.reviews[review_id]
    assert (row["status"], row["app"], row["target_env"], row["pr_head_sha"]) == ("received", "sample-app", "aws", HEAD)
    [(topic, key, value)] = env.publisher.sent
    assert (topic, key) == ("review.requested", REPO)
    msg = ReviewRequested.model_validate_json(value)
    assert (msg.review_id, msg.spec_ref.commit, msg.requested_by) == (review_id, HEAD, "hyeyeon")


def test_invalid_spec_is_422_with_errors(env: Env) -> None:
    spec = yaml.safe_load(sample_text())
    spec["runtime"]["port"] = "eighty"
    del spec["image"]
    env.put_spec(yaml.safe_dump(spec))
    resp = env.request_review()

    assert resp.status_code == 422
    locs = {tuple(e["loc"]) for e in resp.json()["detail"]["errors"]}
    assert ("image",) in locs and ("runtime", "port") in locs
    assert env.repo.reviews == {} and env.publisher.sent == []


def test_not_yaml_is_422(env: Env) -> None:
    env.put_spec("a: [unclosed")
    assert env.request_review().status_code == 422


def test_missing_spec_file_is_404(env: Env) -> None:
    assert env.request_review().status_code == 404


def test_bad_request_shape_is_422(env: Env) -> None:
    resp = env.client.post("/reviews", json={"spec_ref": {"repository": "no-slash", "commit": "zz"}})
    assert resp.status_code == 422


def test_publish_failure_marks_failed(env: Env) -> None:
    async def boom(*_: Any) -> None:
        raise RuntimeError("kafka down")

    env.publisher.send = boom  # type: ignore[method-assign]
    env.put_spec(sample_text())
    resp = env.request_review()

    assert resp.status_code == 503
    [row] = env.repo.reviews.values()
    assert row["status"] == "failed"


def test_review_id_format() -> None:
    rid = new_review_id(datetime(2026, 10, 8, tzinfo=UTC))
    assert rid.startswith("rv_20261008_") and len(rid) == len("rv_20261008_") + 8


# --- GET /reviews/{id}, /verify --------------------------------------------------------------------

async def test_get_review_and_verify(env: Env) -> None:
    env.put_spec(sample_text())
    rid = env.request_review().json()["review_id"]

    assert env.client.get(f"/reviews/{rid}").json()["status"] == "received"
    assert env.client.get("/verify", params={"sha": HEAD}).json()["passed"] is False

    await env.set_status(rid, status="waiting_ci", verdict="pass")
    body = env.client.get("/verify", params={"sha": HEAD}).json()
    assert (body["passed"], body["verdict"], body["review_id"]) == (True, "pass", rid)


def test_unknown_review_and_sha_are_404(env: Env) -> None:
    assert env.client.get("/reviews/rv_x").status_code == 404
    assert env.client.get("/verify", params={"sha": "b" * 40}).status_code == 404


# --- POST /reviews/{id}/decision -------------------------------------------------------------------

async def test_decision_only_when_needs_human(env: Env) -> None:
    env.put_spec(sample_text())
    rid = env.request_review().json()["review_id"]
    decision = {"decision": "approved", "approver": "hyeyeon",
                "edited_ops": [{"op": "replace", "path": "/runtime/replicas", "value": 1}]}

    assert env.client.post(f"/reviews/{rid}/decision", json=decision).status_code == 409

    await env.set_status(rid, status="needs_human", final_spec=yaml.safe_load(sample_text()))
    assert env.client.post(f"/reviews/{rid}/decision", json=decision).status_code == 202
    topic, key, value = env.publisher.sent[-1]
    msg = parse_review_resumed(value)
    assert (topic, key, msg.kind) == ("review.resumed", rid, "human_decision")
    assert msg.human_decision.as_state()["edited_ops"] == decision["edited_ops"]


async def test_decision_rejects_bad_ops(env: Env) -> None:
    env.put_spec(sample_text())
    rid = env.request_review().json()["review_id"]
    await env.set_status(rid, status="needs_human")
    resp = env.client.post(f"/reviews/{rid}/decision",
                           json={"decision": "approved", "approver": "x", "edited_ops": [{"op": "move", "path": "a"}]})
    assert resp.status_code == 422


@pytest.mark.parametrize("ops", [
    [{"op": "replace", "path": "/runtime/no_such_field", "value": 1}],  # 경로 없음
    [{"op": "replace", "path": "/runtime/replicas", "value": "many"}],  # 명세 형식을 깸
])
async def test_decision_edited_ops_checked_against_paused_spec(env: Env, ops: list) -> None:
    """check_edited_ops 로 멈춘 명세(final_spec)에 미리 적용해 보고, 안 되면 재개 전에 422."""
    env.put_spec(sample_text())
    rid = env.request_review().json()["review_id"]
    await env.set_status(rid, status="needs_human", final_spec=yaml.safe_load(sample_text()))
    sent = len(env.publisher.sent)
    resp = env.client.post(f"/reviews/{rid}/decision", json={"decision": "approved", "approver": "x", "edited_ops": ops})

    assert resp.status_code == 422
    assert len(env.publisher.sent) == sent

    rejected = env.client.post(f"/reviews/{rid}/decision", json={"decision": "rejected", "approver": "x", "edited_ops": ops})
    assert rejected.status_code == 202  # 거절이면 ops 를 보지 않는다


# --- POST /webhooks/github -------------------------------------------------------------------------


async def _paused_with_recommendations(env: Env, name: str = "04-human-engine-change-with-data.yaml") -> str:
    from review_ai.graph import initial_state, run_graph
    from review_ai.judge.fake_llm import FAKES
    from review_ai.retrieval.file_retriever import FileRetriever

    spec = yaml.safe_load(sample_text(name))
    paused = await run_graph(initial_state(spec, review_id="paused"), llm=FAKES["oracle"](), retriever=FileRetriever())
    env.put_spec(sample_text(name))
    rid = env.request_review().json()["review_id"]
    await env.set_status(rid, status="needs_human", decision=paused["decision"],
                         final_spec={k: v for k, v in paused["deploy_spec"].items() if k != "baseline"})
    return rid


async def test_approval_without_values_uses_server_recommendations(env: Env) -> None:
    rid = await _paused_with_recommendations(env)
    displayed = env.client.get(f"/reviews/{rid}").json()
    assert displayed["decision"]["recommendations"][0]["source"] == "baseline"
    response = env.client.post(f"/reviews/{rid}/decision", json={"decision": "approved", "approver": "tester"})
    assert response.status_code == 202
    msg = parse_review_resumed(env.publisher.sent[-1][2])
    assert msg.human_decision.use_recommendations and not msg.human_decision.edited_ops


async def test_approval_without_values_and_no_recommendations_is_accepted(env: Env) -> None:
    """권장값을 못 만드는 사유(평문 비밀)여도 값 없는 승인은 그대로 승인으로 받는다 — 422 로 막지 않는다."""
    rid = await _paused_with_recommendations(env, "06-human-plaintext-secret.yaml")
    assert not env.client.get(f"/reviews/{rid}").json()["decision"]["recommendations"]

    response = env.client.post(f"/reviews/{rid}/decision", json={"decision": "approved", "approver": "tester"})

    assert response.status_code == 202
    assert not parse_review_resumed(env.publisher.sent[-1][2]).human_decision.edited_ops


@pytest.mark.parametrize("op", [
    {"op": "replace", "path": "/database/version"},
    {"op": "replace", "path": "/database/version", "value": ""},
])
async def test_partial_answer_keeps_unanswered_value_through_message(env: Env, op: dict) -> None:
    rid = await _paused_with_recommendations(env)
    response = env.client.post(f"/reviews/{rid}/decision", json={"decision": "approved", "approver": "tester",
                              "edited_ops": [{"op": "replace", "path": "/database/engine", "value": "postgres"}, op]})
    assert response.status_code == 202
    msg = parse_review_resumed(env.publisher.sent[-1][2])
    assert msg.human_decision.as_state()["edited_ops"][1] == op


async def test_explicit_null_is_distinct_from_omitted_value(env: Env) -> None:
    rid = await _paused_with_recommendations(env)
    response = env.client.post(f"/reviews/{rid}/decision", json={"decision": "approved", "approver": "tester",
                              "edited_ops": [{"op": "replace", "path": "/database/version", "value": None}]})
    assert response.status_code == 202
    msg = parse_review_resumed(env.publisher.sent[-1][2])
    assert msg.human_decision.as_state()["edited_ops"] == [{"op": "replace", "path": "/database/version", "value": None}]


async def test_unanswered_unknown_value_does_not_publish(env: Env) -> None:
    rid = await _paused_with_recommendations(env, "10-human-mixed-aws.yaml")
    before = len(env.publisher.sent)
    response = env.client.post(f"/reviews/{rid}/decision", json={"decision": "approved", "approver": "tester",
                              "edited_ops": [{"op": "add", "path": "/runtime/health/readiness"}]})
    assert response.status_code == 422
    assert len(env.publisher.sent) == before


@pytest.mark.parametrize("path", ["/storage/volumes/0/size/typo", "/storage/volumes/2/size"])
async def test_unanswered_invalid_path_does_not_publish(env: Env, path: str) -> None:
    rid = await _paused_with_recommendations(env, "08-human-volume-shrink-local.yaml")
    before = len(env.publisher.sent)
    response = env.client.post(f"/reviews/{rid}/decision", json={"decision": "approved", "approver": "tester",
                              "edited_ops": [{"op": "replace", "path": path}]})
    assert response.status_code == 422
    assert len(env.publisher.sent) == before
    assert env.repo.reviews[rid]["status"] == "needs_human"


async def test_client_cannot_supply_server_default_audit(env: Env) -> None:
    rid = await _paused_with_recommendations(env)
    before = len(env.publisher.sent)
    response = env.client.post(f"/reviews/{rid}/decision", json={"decision": "approved", "approver": "tester",
                              "defaulted_ops": []})
    assert response.status_code == 422
    assert len(env.publisher.sent) == before


async def test_unanswered_field_without_recommendation_is_422(env: Env) -> None:
    """값을 비운 항목에 권장값이 없으면 채울 수 없다 — 422, 재개 메시지도 보내지 않는다."""
    rid = await _paused_with_recommendations(env, "06-human-plaintext-secret.yaml")
    before = len(env.publisher.sent)
    body = {"decision": "approved", "approver": "tester",
            "edited_ops": [{"op": "add", "path": "/runtime/health/readiness"}]}
    response = env.client.post(f"/reviews/{rid}/decision", json=body)
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert "/runtime/health/readiness" in errors and "use_recommendations: false" in errors
    assert len(env.publisher.sent) == before


def check_suite(conclusion: str = "success", slug: str = "github-actions", action: str = "completed") -> dict:
    return {"action": action, "check_suite": {"head_sha": HEAD, "conclusion": conclusion, "app": {"slug": slug}}}


async def test_check_suite_completed_resumes_waiting_review(env: Env) -> None:
    env.put_spec(sample_text())
    rid = env.request_review().json()["review_id"]
    await env.set_status(rid, status="waiting_ci")
    raw, sig = signed(check_suite("failure"))
    resp = env.client.post("/webhooks/github", content=raw,
                           headers={"X-GitHub-Event": "check_suite", "X-Hub-Signature-256": sig})

    assert resp.json() == {"resumed": [rid]}
    msg = parse_review_resumed(env.publisher.sent[-1][2])
    assert (msg.kind, msg.ci.head_sha, msg.ci.conclusion) == ("ci_completed", HEAD, "failure")


def test_github_webhook_bad_signature_is_401(env: Env) -> None:
    raw, _ = signed(check_suite())
    resp = env.client.post("/webhooks/github", content=raw,
                           headers={"X-GitHub-Event": "check_suite", "X-Hub-Signature-256": "sha256=00"})
    assert resp.status_code == 401


@pytest.mark.parametrize(("event", "body"), [
    ("push", check_suite()),
    ("check_suite", check_suite(action="requested")),
    ("check_suite", check_suite(slug="some-other-app")),
])
async def test_github_webhook_ignores_other_events(env: Env, event: str, body: dict) -> None:
    env.put_spec(sample_text())
    rid = env.request_review().json()["review_id"]
    await env.set_status(rid, status="waiting_ci")
    before = len(env.publisher.sent)
    raw, sig = signed(body)
    resp = env.client.post("/webhooks/github", content=raw,
                           headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sig})

    assert resp.status_code == 202 and "ignored" in resp.json()
    assert len(env.publisher.sent) == before


# --- POST /webhooks/argocd -------------------------------------------------------------------------

MERGE = "c0ffee1" + "0" * 33


async def _merged_review(env: Env) -> str:
    env.put_spec(sample_text())
    rid = env.request_review().json()["review_id"]
    await env.set_status(rid, status="blocked", merge_sha=MERGE, final_spec=yaml.safe_load(sample_text()))
    return rid


def argo(health: str, tag: str) -> dict[str, Any]:
    return {"app": "sample-app", "env": "aws", "health": health,
            "images": [f"ghcr.io/crystal-sbhackathon2026/sample-app:{tag}"]}


async def test_argocd_healthy_with_merge_sha_updates_baseline(env: Env) -> None:
    rid = await _merged_review(env)
    resp = env.client.post("/webhooks/argocd", json=argo("Healthy", MERGE[:7]),
                           headers={"Authorization": "Bearer argo-token"})

    assert resp.json() == {"review_id": rid, "recorded": "healthy"}
    assert [e["kind"] for e in env.repo.deploy_events] == ["healthy"]
    baseline = env.repo.baselines[("sample-app", "aws")]
    assert (baseline["merge_sha"], baseline["database_has_data"]) == (MERGE, None)
    assert baseline["spec"]["metadata"]["name"] == "sample-app"


async def test_argocd_old_image_after_merge_is_ignored(env: Env) -> None:
    """병합 직후 ① 새 설정 + 옛 이미지 배포는 이미지 태그가 merge_sha 와 달라 기록하지 않는다."""
    await _merged_review(env)
    resp = env.client.post("/webhooks/argocd", json=argo("Healthy", "b084c24"),
                           headers={"Authorization": "Bearer argo-token"})

    assert "ignored" in resp.json()
    assert env.repo.deploy_events == [] and env.repo.baselines == {}


async def test_argocd_degraded_records_event_only(env: Env) -> None:
    await _merged_review(env)
    env.client.post("/webhooks/argocd", json=argo("Degraded", MERGE[:7]), headers={"Authorization": "Bearer argo-token"})

    assert [e["kind"] for e in env.repo.deploy_events] == ["degraded"]
    assert env.repo.baselines == {}


async def test_argocd_requires_token(env: Env) -> None:
    await _merged_review(env)
    resp = env.client.post("/webhooks/argocd", json=argo("Healthy", MERGE[:7]))
    assert resp.status_code == 401


# --- POST /webhooks/github — pull_request 로 검토 시작 ----------------------------------------------

def pr_event(action: str = "opened", sha: str = HEAD, number: int = 5, base: str = "main") -> dict[str, Any]:
    return {"action": action, "number": number, "sender": {"login": "octo-dev"},
            "repository": {"full_name": REPO, "default_branch": "main"},
            "pull_request": {"number": number, "head": {"sha": sha, "ref": "feature"}, "base": {"ref": base}}}


def send_pr(env: Env, body: dict[str, Any]) -> Any:
    raw, sig = signed(body)
    return env.client.post("/webhooks/github", content=raw,
                           headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig})


def test_pr_opened_starts_review(env: Env) -> None:
    env.put_spec(sample_text())
    resp = send_pr(env, pr_event("opened"))

    assert resp.status_code == 202
    rid = resp.json()["review_id"]
    row = env.repo.reviews[rid]
    assert (row["status"], row["pr_head_sha"], row["pr_number"], row["requested_by"]) == ("received", HEAD, 5, "octo-dev")
    [(topic, _, value)] = env.publisher.sent
    assert topic == "review.requested" and ReviewRequested.model_validate_json(value).requested_by == "octo-dev"


def test_pr_without_deploy_yaml_is_skipped(env: Env) -> None:
    resp = send_pr(env, pr_event("opened"))

    assert (resp.status_code, resp.json()) == (202, {"skipped": "no deploy.yaml"})
    assert env.repo.reviews == {} and env.publisher.sent == []


def test_same_sha_twice_makes_one_review(env: Env) -> None:
    """워커 commit_fix 의 커밋에 synchronize 가 와도, 워커가 먼저 넣어 둔 검토가 있으면 새로 만들지 않는다."""
    env.put_spec(sample_text())
    first = send_pr(env, pr_event("opened")).json()["review_id"]
    second = send_pr(env, pr_event("synchronize")).json()

    assert second == {"skipped": "already reviewed", "review_id": first}
    assert len(env.repo.reviews) == 1 and len(env.publisher.sent) == 1


async def test_synchronize_supersedes_open_reviews_of_same_pr(env: Env) -> None:
    new_sha = "b" * 40
    env.put_spec(sample_text())
    env.put_spec(sample_text(), sha=new_sha)
    old = send_pr(env, pr_event("opened")).json()["review_id"]
    await env.set_status(old, status="needs_human")
    await env.repo.insert_review(review_id="rv_done", app="sample-app", target_env="aws", repo_id=REPO,
                                 spec_ref={"repository": REPO, "commit": "c" * 40, "path": "deploy.yaml"},
                                 pr_head_sha="c" * 40, requested_by="x", pr_number=5)
    await env.set_status("rv_done", status="committed")
    await env.repo.insert_review(review_id="rv_other_pr", app="sample-app", target_env="aws", repo_id=REPO,
                                 spec_ref={"repository": REPO, "commit": "d" * 40, "path": "deploy.yaml"},
                                 pr_head_sha="d" * 40, requested_by="x", pr_number=6)

    body = send_pr(env, pr_event("synchronize", sha=new_sha)).json()

    new = body["review_id"]
    assert body["superseded"] == [old]
    assert (env.repo.reviews[old]["status"], env.repo.reviews[old]["superseded_by"]) == ("superseded", new)
    assert env.repo.reviews["rv_done"]["status"] == "committed"  # 끝난 검토는 그대로
    assert env.repo.reviews["rv_other_pr"]["status"] == "received"  # 다른 PR 은 그대로
    assert env.repo.reviews[new]["status"] == "received"


async def test_superseded_status_is_not_overwritten_by_worker(env: Env) -> None:
    """워커가 돌던 검토가 새 커밋에 밀려 superseded 가 된 뒤, 워커의 상태 기록이 그걸 덮어쓰지 않는다."""
    env.put_spec(sample_text())
    rid = send_pr(env, pr_event("opened")).json()["review_id"]
    await env.repo.supersede_open(repository=REPO, pr_number=5, superseded_by="rv_newer")
    await env.repo.update_review(rid, status="waiting_ci", verdict="pass")

    assert (env.repo.reviews[rid]["status"], env.repo.reviews[rid]["verdict"]) == ("superseded", "pass")


@pytest.mark.parametrize("body", [
    pr_event("closed"),
    pr_event("edited"),
    pr_event("opened", base="release"),  # 기본 브랜치가 아닌 PR
])
def test_pr_events_ignored(env: Env, body: dict[str, Any]) -> None:
    env.put_spec(sample_text())
    resp = send_pr(env, body)

    assert resp.status_code == 202 and "ignored" in resp.json()
    assert env.repo.reviews == {}


def test_pr_webhook_bad_signature_is_401(env: Env) -> None:
    raw, _ = signed(pr_event())
    resp = env.client.post("/webhooks/github", content=raw,
                           headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=00"})
    assert resp.status_code == 401
