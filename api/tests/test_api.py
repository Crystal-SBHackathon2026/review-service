"""Review API — 샘플 01 은 202, 형식 오류 명세는 422. 나머지 엔드포인트와 웹훅."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from review_ai.errors import TransientError
from review_ai.messages import ReviewRequested
from review_api.app import ApiDeps, create_app
from review_common.ids import new_review_id
from review_common.github import SpecNotFound
from review_common.kafka import DEFAULT_SEND_TIMEOUT, KafkaPublisher
from review_common.repository import InMemoryReviewRepository
from review_common.resumed import parse_review_resumed

SAMPLES = Path(__file__).resolve().parents[2] / "ai" / "samples"
REPO = "Crystal-SBHackathon2026/sample-app"
HEAD = "a" * 40
SECRET = "webhook-secret"
API_TOKEN = "api-token"


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
                                      github_webhook_secret=SECRET, argocd_webhook_token="argo-token",
                                      api_token=API_TOKEN))
        self.client = TestClient(self.app, headers={"Authorization": f"Bearer {API_TOKEN}"})

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


@pytest.mark.parametrize("commit", [HEAD[:7], HEAD[:39], HEAD + "a", HEAD.upper()])
def test_short_sha_accepted_by_api(env: Env, commit: str) -> None:
    """P1-7 — 짧은 SHA 를 접수하면 병합 단계(merge_pull 의 sha)에서야 실패했다. 이제 접수할 때 422."""
    env.put_spec(sample_text(), sha=commit)
    resp = env.request_review(sha=commit)

    assert resp.status_code == 422
    assert env.repo.reviews == {} and env.publisher.sent == []


def test_publish_failure_marks_failed(env: Env) -> None:
    async def boom(*_: Any) -> None:
        raise RuntimeError("kafka down")

    env.publisher.send = boom  # type: ignore[method-assign]
    env.put_spec(sample_text())
    resp = env.request_review()

    assert resp.status_code == 503
    [row] = env.repo.reviews.values()
    assert row["status"] == "failed"


class StuckProducer:
    """MSK 가 응답하지 않는다 — send_and_wait 가 끝나지 않는다."""

    async def send_and_wait(self, topic: str, value: bytes | None = None, key: bytes | None = None) -> None:
        await asyncio.Event().wait()


def test_kafka_send_timeout_default_fits_github_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAFKA_SEND_TIMEOUT", raising=False)
    assert KafkaPublisher(StuckProducer()).timeout == DEFAULT_SEND_TIMEOUT <= 5  # GitHub 웹훅은 10초 안에 응답해야 한다
    monkeypatch.setenv("KAFKA_SEND_TIMEOUT", "2.5")
    assert KafkaPublisher(StuckProducer()).timeout == 2.5


async def test_stuck_kafka_send_raises_transient_error() -> None:
    with pytest.raises(TransientError, match="review.requested"):
        await KafkaPublisher(StuckProducer(), timeout=0.05).send("review.requested", "k", b"v")


@pytest.mark.parametrize("route", ["review", "decision", "check_suite"])
async def test_stuck_kafka_returns_503_within_timeout(env: Env, route: str) -> None:
    """발행이 멈추면 웹훅·API 가 KAFKA_SEND_TIMEOUT 안에 503 을 준다 (P1-6)."""
    env.publisher = KafkaPublisher(StuckProducer(), timeout=0.2)  # type: ignore[assignment]
    env.app.state.deps.publisher = env.publisher
    env.put_spec(sample_text())
    rid = new_review_id()
    if route != "review":
        await env.repo.insert_review(review_id=rid, app="sample-app", target_env="aws", repo_id=REPO,
                                     spec_ref={"repository": REPO, "commit": HEAD, "path": "deploy.yaml"},
                                     pr_head_sha=HEAD, requested_by="t")
        await env.set_status(rid, status="needs_human" if route == "decision" else "waiting_ci")

    started = time.monotonic()
    if route == "review":
        resp = env.request_review()
    elif route == "decision":
        resp = env.client.post(f"/reviews/{rid}/decision", json={"decision": "rejected", "approver": "h"})
    else:
        raw, sig = signed({"action": "completed", "check_suite": {
            "head_sha": HEAD, "conclusion": "success", "app": {"slug": "github-actions"}}})
        resp = env.client.post("/webhooks/github", content=raw,
                               headers={"X-GitHub-Event": "check_suite", "X-Hub-Signature-256": sig})

    assert resp.status_code == 503
    assert time.monotonic() - started < 5


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


# --- GET /reviews, /ui (사람 승인 화면) -------------------------------------------------------------

async def _listed(env: Env, statuses: list[str]) -> list[str]:
    """statuses 순서대로 검토를 만든다. created_at 은 한 시간씩 뒤 — 마지막이 가장 최근."""
    ids = []
    for i, status in enumerate(statuses):
        rid, sha = f"rv_20261009_{i:08x}", f"{i:x}" * 40  # 같은 SHA 검토는 하나뿐이다 (0008)
        await env.repo.insert_review(review_id=rid, app="sample-app", target_env="aws", repo_id=REPO,
                                     spec_ref={"repository": REPO, "commit": sha, "path": "deploy.yaml"},
                                     pr_head_sha=sha, requested_by="hyeyeon", pr_number=5)
        env.repo.reviews[rid]["created_at"] = datetime(2026, 10, 9, i, tzinfo=UTC)
        await env.set_status(rid, status=status)
        ids.append(rid)
    return ids


async def test_list_reviews_filters_status_newest_first(env: Env) -> None:
    ids = await _listed(env, ["needs_human", "committed", "waiting_ci", "needs_human", "reviewing"])
    anonymous = TestClient(env.app)  # 읽기는 토큰 없이

    one = anonymous.get("/reviews", params={"status": "needs_human"})
    many = anonymous.get("/reviews", params=[("status", "needs_human"), ("status", "waiting_ci")])

    assert one.status_code == 200
    assert [r["review_id"] for r in one.json()] == [ids[3], ids[0]]
    assert [r["review_id"] for r in many.json()] == [ids[3], ids[2], ids[0]]
    assert [r["review_id"] for r in anonymous.get("/reviews").json()] == ids[::-1]
    assert set(one.json()[0]) == {"review_id", "app", "target_env", "spec_ref", "status", "verdict", "reasons",
                                  "requested_by", "created_at", "updated_at", "pr_number"}
    assert one.json()[0]["pr_number"] == 5
    assert anonymous.get(f"/reviews/{ids[0]}").json()["pr_number"] == 5  # 상세 화면의 PR 링크


async def test_list_reviews_limit(env: Env) -> None:
    ids = await _listed(env, ["needs_human"] * 4)

    assert [r["review_id"] for r in env.client.get("/reviews", params={"limit": 2}).json()] == [ids[3], ids[2]]
    assert env.client.get("/reviews", params={"limit": 0}).status_code == 422
    assert env.client.get("/reviews", params={"limit": 201}).status_code == 422


def test_list_reviews_unknown_status_is_422(env: Env) -> None:
    assert env.client.get("/reviews", params={"status": "needs-human"}).status_code == 422


def test_ui_serves_html_without_token(env: Env) -> None:
    resp = TestClient(env.app).get("/ui")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<!doctype html>" in resp.text.lower()
    assert "/decision" in resp.text
    assert "connect-src 'self'" in resp.headers["content-security-policy"]


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

    assert resp.json() == {"review_id": rid, "recorded": "healthy", "baseline": "updated"}
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
    assert env.repo.cases == {}  # 판단이 필요했던 finding 이 없던 검토 — 남길 사례가 없다


DB_FINDING = {"finding_id": "DB-001:abc", "rule_id": "DB-001", "severity": "high", "title": "DB 엔진 변경",
              "location": {"spec_path": "/database/engine"}, "evidence": "mysql"}


async def test_argocd_degraded_records_case_of_decided_review(env: Env) -> None:
    rid = await _merged_review(env)
    await env.set_status(rid, findings=[DB_FINDING], decision={"reasons": ["IRREVERSIBLE"]})
    for _ in range(2):  # Argo CD 는 같은 상태를 여러 번 보낸다
        env.client.post("/webhooks/argocd", json=argo("Degraded", MERGE[:7]),
                        headers={"Authorization": "Bearer argo-token"})

    [case] = env.repo.cases.values()
    assert (case["case_id"], case["rule_ids"], case["outcome"]) == (f"{rid}.deploy_degraded", ["DB-001"],
                                                                     "deploy_degraded")
    assert "Degraded" in case["summary"]


async def test_argocd_degraded_autofix_review_records_original_review(env: Env) -> None:
    """병합된 건 AI 수정 커밋의 재검토(finding 없음) — 판단은 원래 검토에 있다."""
    env.put_spec(sample_text())
    original = env.request_review().json()["review_id"]
    fix_round = {"findings": [DB_FINDING], "reasons": [],
                 "patch": {"ops": [{"op": "replace", "path": "/database/engine", "value": "postgres"}]}}
    await env.set_status(original, status="superseded", rounds=[fix_round])
    await env.repo.insert_review(review_id="rv_autofix", app="sample-app", target_env="aws", repo_id=REPO,
                                 spec_ref={"repository": REPO, "commit": MERGE, "path": "deploy.yaml"},
                                 pr_head_sha=MERGE, requested_by=f"autofix:{original}")
    await env.set_status("rv_autofix", status="committed", merge_sha=MERGE)

    resp = env.client.post("/webhooks/argocd", json=argo("Degraded", MERGE[:7]),
                           headers={"Authorization": "Bearer argo-token"})

    assert resp.json() == {"review_id": "rv_autofix", "recorded": "degraded"}
    case = env.repo.cases[f"{original}.deploy_degraded"]
    assert case["ops"] == [{"op": "replace", "path": "/database/engine", "value": "postgres"}]


async def test_argocd_degraded_case_failure_keeps_event(env: Env) -> None:
    rid = await _merged_review(env)
    await env.set_status(rid, findings=[DB_FINDING])

    async def broken(**_: Any) -> bool:
        raise RuntimeError("db down")

    env.repo.insert_case = broken  # type: ignore[method-assign]
    resp = env.client.post("/webhooks/argocd", json=argo("Degraded", MERGE[:7]),
                           headers={"Authorization": "Bearer argo-token"})

    assert resp.json() == {"review_id": rid, "recorded": "degraded"}
    assert [e["kind"] for e in env.repo.deploy_events] == ["degraded"]


async def test_argocd_requires_token(env: Env) -> None:
    await _merged_review(env)
    resp = env.client.post("/webhooks/argocd", json=argo("Healthy", MERGE[:7]))
    assert resp.status_code == 401


# --- POST /webhooks/github — pull_request 로 검토 시작 ----------------------------------------------

def pr_event(action: str = "opened", sha: str = HEAD, number: int = 5, base: str = "main",
             head_repo: str = REPO) -> dict[str, Any]:
    return {"action": action, "number": number, "sender": {"login": "octo-dev"},
            "repository": {"full_name": REPO, "default_branch": "main"},
            "pull_request": {"number": number, "base": {"ref": base, "repo": {"full_name": REPO}},
                             "head": {"sha": sha, "ref": "feature", "repo": {"full_name": head_repo}}}}


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


def test_same_sha_twice_makes_one_review(env: Env) -> None:
    """워커 commit_fix 의 커밋에 synchronize 가 와도, 워커가 먼저 넣어 둔 검토가 있으면 새로 만들지 않는다."""
    env.put_spec(sample_text())
    first = send_pr(env, pr_event("opened")).json()["review_id"]
    second = send_pr(env, pr_event("synchronize")).json()

    assert second == {"skipped": "already reviewed", "review_id": first}
    assert len(env.repo.reviews) == 1 and len(env.publisher.sent) == 1


async def test_same_webhook_twice_at_once_makes_one_review(env: Env) -> None:
    """P2 — 같은 웹훅 두 개가 동시에 와서 둘 다 find_by_head 를 지나도 검토는 하나, 발행도 한 번."""
    env.put_spec(sample_text())
    both_fetching = asyncio.Barrier(2)
    get_file = env.specs.get_file

    async def slow_get_file(repository: str, path: str, ref: str) -> str:
        await both_fetching.wait()  # 둘 다 find_by_head 를 지난 뒤에 넣는다
        return await get_file(repository, path, ref)

    env.specs.get_file = slow_get_file  # type: ignore[method-assign]
    raw, sig = signed(pr_event("opened"))
    headers = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://api") as client:
        a, b = await asyncio.gather(client.post("/webhooks/github", content=raw, headers=headers),
                                    client.post("/webhooks/github", content=raw, headers=headers))

    assert a.status_code == b.status_code == 202
    assert a.json()["review_id"] == b.json()["review_id"]
    assert len(env.repo.reviews) == 1 and len(env.publisher.sent) == 1


async def test_same_sha_after_failed_review_is_reviewed_again(env: Env) -> None:
    env.put_spec(sample_text())
    first = env.request_review().json()["review_id"]
    await env.set_status(first, status="failed")

    second = env.request_review().json()["review_id"]

    assert second != first and len(env.publisher.sent) == 2


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


def closed_event(*, merged: bool, number: int = 5) -> dict[str, Any]:
    body = pr_event("closed", number=number)
    body["pull_request"]["merged"] = merged
    return body


async def test_closed_pr_supersedes_open_reviews_and_leaves_ui_list(env: Env) -> None:
    """P2 — needs_human 검토가 있는 PR 을 결정 없이 닫으면 superseded, 승인 화면 목록(GET /reviews)에서 빠진다."""
    env.put_spec(sample_text())
    rid = send_pr(env, pr_event("opened")).json()["review_id"]
    await env.set_status(rid, status="needs_human")
    await env.repo.insert_review(review_id="rv_done", app="sample-app", target_env="aws", repo_id=REPO,
                                 spec_ref={"repository": REPO, "commit": "c" * 40, "path": "deploy.yaml"},
                                 pr_head_sha="c" * 40, requested_by="x", pr_number=5)
    await env.set_status("rv_done", status="committed")
    assert [r["review_id"] for r in env.client.get("/reviews", params={"status": "needs_human"}).json()] == [rid]

    resp = send_pr(env, closed_event(merged=False))

    assert resp.json() == {"closed": 5, "superseded": [rid]}
    row = env.repo.reviews[rid]
    assert (row["status"], row["error"], row["superseded_by"]) == ("superseded", "PR closed", None)
    assert env.repo.reviews["rv_done"]["status"] == "committed"
    assert env.client.get("/reviews", params={"status": "needs_human"}).json() == []
    # 멈춘 그래프에 사람 결정이 와도 받지 않는다
    resp = env.client.post(f"/reviews/{rid}/decision", json={"decision": "approved", "approver": "h"})
    assert resp.status_code == 409


async def test_merged_close_does_nothing(env: Env) -> None:
    env.put_spec(sample_text())
    rid = send_pr(env, pr_event("opened")).json()["review_id"]
    await env.set_status(rid, status="waiting_ci")

    assert send_pr(env, closed_event(merged=True)).json() == {"ignored": "merged"}
    assert env.repo.reviews[rid]["status"] == "waiting_ci"


def test_reopened_after_close_reviews_same_sha_again(env: Env) -> None:
    env.put_spec(sample_text())
    first = send_pr(env, pr_event("opened")).json()["review_id"]
    send_pr(env, closed_event(merged=False))

    body = send_pr(env, pr_event("reopened")).json()

    assert body["review_id"] != first
    assert env.repo.reviews[body["review_id"]]["status"] == "received"
    assert len(env.publisher.sent) == 2


@pytest.mark.parametrize("body", [
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



# --- 포크 PR (P0-2) ------------------------------------------------------------------------------------

@pytest.mark.parametrize("head_repo", ["someone/sample-app", None])
def test_fork_pr_is_skipped(env: Env, head_repo: str | None) -> None:
    """포크(다른 레포)이거나 head 레포가 없으면(포크가 지워짐) 검토도 intake 도 하지 않는다."""
    env.put_spec(sample_text())
    body = pr_event("opened")
    body["pull_request"]["head"]["repo"] = {"full_name": head_repo} if head_repo else None
    resp = send_pr(env, body)

    assert (resp.status_code, resp.json()) == (202, {"skipped": "fork"})
    assert env.repo.reviews == {} and env.publisher.sent == []



# --- 승인 API 토큰 인증 (P0-1) ---------------------------------------------------------------------------

def _decision_target(env: Env) -> str:
    env.put_spec(sample_text())
    return env.client.post("/reviews", json={"spec_ref": {"repository": REPO, "commit": HEAD}, "requested_by": "x"}
                           ).json()["review_id"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": API_TOKEN}])
async def test_post_routes_need_token(env: Env, headers: dict[str, str]) -> None:
    """토큰 없음·틀림·Bearer 빠짐은 401 — 아무것도 바뀌지 않는다."""
    rid = _decision_target(env)
    await env.set_status(rid, status="needs_human", final_spec=yaml.safe_load(sample_text()))
    anon = TestClient(env.app, headers=headers)
    sent = len(env.publisher.sent)

    review = anon.post("/reviews", json={"spec_ref": {"repository": REPO, "commit": HEAD}, "requested_by": "x"})
    decision = anon.post(f"/reviews/{rid}/decision", json={"decision": "approved", "approver": "intruder"})

    assert (review.status_code, decision.status_code) == (401, 401)
    assert len(env.publisher.sent) == sent and len(env.repo.reviews) == 1


async def test_right_token_is_accepted(env: Env) -> None:
    rid = _decision_target(env)
    await env.set_status(rid, status="needs_human", final_spec=yaml.safe_load(sample_text()))
    resp = env.client.post(f"/reviews/{rid}/decision", json={"decision": "rejected", "approver": "hyeyeon"})

    assert resp.status_code == 202


def test_routes_fail_closed_without_configured_tokens() -> None:
    """토큰 환경변수가 비어 있으면 검사를 건너뛰지 않고 503. GET·헬스체크는 그대로."""
    app = create_app(ApiDeps(repo=InMemoryReviewRepository(), specs=FakeSpecs(), publisher=FakePublisher(),
                             github_webhook_secret=SECRET))
    client = TestClient(app, headers={"Authorization": "Bearer anything"})

    assert client.post("/reviews", json={"spec_ref": {"repository": REPO, "commit": HEAD},
                                         "requested_by": "x"}).status_code == 503
    assert client.post("/reviews/rv_x/decision", json={"decision": "approved", "approver": "x"}).status_code == 503
    assert client.post("/webhooks/argocd", json={"app": "sample-app", "env": "aws", "health": "Healthy",
                                                 "images": []}).status_code == 503
    assert client.get("/healthz").status_code == 200
    assert client.get("/reviews/rv_x").status_code == 404
