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
from review_api.app import ApiDeps, create_app, new_review_id
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

    await env.set_status(rid, status="needs_human")
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


# --- POST /webhooks/github -------------------------------------------------------------------------

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
