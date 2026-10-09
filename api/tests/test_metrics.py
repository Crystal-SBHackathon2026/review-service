"""GET /metrics — HTTP·웹훅·sweep 카운터, 업무 DB 게이지. 카운터는 프로세스 전체에서 쌓이므로 증가분으로 본다."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from review_api import metrics
from review_api.recovery import MAX_RECOVERIES, recover_stale_reviews
from tests.test_api import HEAD, REPO, Env, pr_event, sample_text, send_pr, signed


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def env() -> Env:
    metrics.DB_STATS.refreshed_at = float("-inf")  # 테스트마다 캐시 없이
    return Env()


def test_metrics_is_open_and_prometheus_text(env: Env) -> None:
    resp = TestClient(env.app).get("/metrics")  # 토큰 없이

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# TYPE review_http_requests_total counter" in resp.text


async def test_request_counts_with_route_template(env: Env) -> None:
    await env.repo.insert_review(review_id="rv_metrics_1", app="sample-app", target_env="aws", repo_id=REPO,
                                 spec_ref={"repository": REPO, "commit": HEAD, "path": "deploy.yaml"},
                                 pr_head_sha=HEAD, requested_by="t")
    labels = {"route": "/reviews/{review_id}", "method": "GET", "status": "200"}
    before = value("review_http_requests_total", **labels)
    hist_before = value("review_http_request_duration_seconds_count", route="/reviews/{review_id}", method="GET")

    env.client.get("/reviews/rv_metrics_1")
    env.client.get("/reviews/rv_metrics_1")
    env.client.get("/reviews/rv_nope")  # 404

    assert value("review_http_requests_total", **labels) == before + 2
    assert value("review_http_requests_total", route="/reviews/{review_id}", method="GET", status="404") >= 1
    assert value("review_http_request_duration_seconds_count", route="/reviews/{review_id}",
                 method="GET") == hist_before + 3
    text = env.client.get("/metrics").text
    assert "rv_metrics_1" not in text and "rv_nope" not in text  # 실제 ID 는 라벨에 없다


def test_probe_and_metrics_routes_are_not_counted(env: Env) -> None:
    for route in ("/healthz", "/readyz", "/metrics"):
        env.client.get(route)
        assert value("review_http_requests_total", route=route, method="GET", status="200") == 0


def test_unknown_path_is_one_label(env: Env) -> None:
    before = value("review_http_requests_total", route="unmatched", method="GET", status="404")
    env.client.get("/wp-admin/abc123")
    env.client.get("/.env")
    assert value("review_http_requests_total", route="unmatched", method="GET", status="404") == before + 2


def test_webhook_results(env: Env) -> None:
    env.put_spec(sample_text())
    before = {r: value("review_webhook_events_total", event="pull_request", result=r) for r in metrics.WEBHOOK_RESULTS}

    send_pr(env, pr_event("opened"))                    # started
    send_pr(env, pr_event("synchronize"))               # 같은 SHA — duplicate
    send_pr(env, pr_event("edited"))                    # skipped
    raw, _ = signed(pr_event())
    env.client.post("/webhooks/github", content=raw,
                    headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=00"})  # rejected

    after = {r: value("review_webhook_events_total", event="pull_request", result=r) for r in metrics.WEBHOOK_RESULTS}
    assert {r: after[r] - before[r] for r in after} == {
        "started": 1, "intake": 0, "skipped": 1, "duplicate": 1, "rejected": 1, "error": 0}


def test_webhook_publish_failure_is_error(env: Env) -> None:
    async def boom(*_: Any) -> None:
        raise RuntimeError("kafka down")

    env.publisher.send = boom  # type: ignore[method-assign]
    env.put_spec(sample_text())
    before = value("review_webhook_events_total", event="pull_request", result="error")

    assert send_pr(env, pr_event("opened")).status_code == 503
    assert value("review_webhook_events_total", event="pull_request", result="error") == before + 1


async def test_db_gauges(env: Env) -> None:
    for i, status in enumerate(["needs_human", "needs_human", "committed"]):
        sha = f"{i}" * 40
        await env.repo.insert_review(review_id=f"rv_g{i}", app="gauge-app", target_env="aws", repo_id=REPO,
                                     spec_ref={"repository": REPO, "commit": sha, "path": "deploy.yaml"},
                                     pr_head_sha=sha, requested_by="t")
        await env.set_status(f"rv_g{i}", status=status)
    env.repo.reviews["rv_g0"]["updated_at"] -= timedelta(minutes=30)

    env.client.get("/metrics")

    assert value("review_reviews", app="gauge-app", env="aws", status="needs_human") == 2
    assert value("review_reviews", app="gauge-app", env="aws", status="committed") == 1
    assert 1790 < value("review_needs_human_oldest_seconds", app="gauge-app", env="aws") < 1900


async def test_db_failure_keeps_last_values_and_metrics_200(env: Env) -> None:
    await env.repo.insert_review(review_id="rv_keep", app="keep-app", target_env="aws", repo_id=REPO,
                                 spec_ref={"repository": REPO, "commit": HEAD, "path": "deploy.yaml"},
                                 pr_head_sha=HEAD, requested_by="t")
    assert env.client.get("/metrics").status_code == 200

    async def db_down(*_: Any) -> Any:
        raise ConnectionError("DB 연결 끊김")

    env.repo.status_counts = db_down  # type: ignore[method-assign]
    metrics.DB_STATS.refreshed_at = float("-inf")  # 캐시가 지났다

    resp = env.client.get("/metrics")
    assert resp.status_code == 200
    assert value("review_reviews", app="keep-app", env="aws", status="received") == 1


async def test_db_stats_cached_for_ttl(env: Env) -> None:
    calls = []

    class Repo:
        async def status_counts(self, since: timedelta) -> list[dict[str, Any]]:
            calls.append(since)
            return []

        async def needs_human_oldest(self) -> list[dict[str, Any]]:
            return []

    stats = metrics.DbStats()
    await stats.refresh(Repo(), now=100.0)
    await stats.refresh(Repo(), now=100.0 + metrics.DB_STATS_TTL - 1)
    await stats.refresh(Repo(), now=100.0 + metrics.DB_STATS_TTL + 1)
    assert len(calls) == 2


async def test_sweep_counters(env: Env) -> None:
    from tests.test_recovery import Env as RecoveryEnv

    renv = RecoveryEnv()
    await renv.review("rv_s1", "reviewing")
    await renv.review("rv_s2", "received", sha="b" * 40)
    renv.repo.reviews["rv_s2"]["recover_count"] = MAX_RECOVERIES
    before = (value("review_sweep_recovered_total", from_status="reviewing"),
              value("review_sweep_recovered_total", from_status="received"), value("review_sweep_failed_total"))

    await recover_stale_reviews(renv.deps, timedelta(minutes=10))

    assert (value("review_sweep_recovered_total", from_status="reviewing"),
            value("review_sweep_recovered_total", from_status="received"),
            value("review_sweep_failed_total")) == (before[0] + 1, before[1] + 1, before[2] + 1)
