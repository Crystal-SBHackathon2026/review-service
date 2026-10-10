"""Review API 지표 — GET /metrics (Prometheus 텍스트 형식). 라이브러리는 prometheus_client 하나.

라벨은 값 종류가 적은 것만 둔다 (route 는 경로 템플릿, app·env·status 등). commit_sha·review_id·레포 전체 경로처럼
끝없이 늘어나는 값은 라벨로 넣지 않는다 — 커밋 단위 추적은 업무 DB 대시보드(커밋 타임라인)로 한다.
지표를 기록하다 실패해도 요청 처리에는 영향이 없어야 한다.

업무 DB 게이지(review_reviews·review_needs_human_oldest_seconds)는 scrape 때 읽는다. DB_STATS_TTL(15초) 동안은
캐시를 쓰고, 쿼리가 실패하면 마지막 값을 그대로 낸다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

log = logging.getLogger(__name__)

HTTP_REQUESTS = Counter("review_http_requests", "Review API HTTP 요청", ["route", "method", "status"])
HTTP_DURATION = Histogram("review_http_request_duration_seconds", "Review API HTTP 처리 시간", ["route", "method"],
                          buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
WEBHOOK_EVENTS = Counter("review_webhook_events", "웹훅 처리 결과", ["event", "result"])
SWEEP_RECOVERED = Counter("review_sweep_recovered", "review sweep 이 회수한 검토 (가져갈 때의 상태)", ["from_status"])
SWEEP_FAILED = Counter("review_sweep_failed", "회수 3번을 넘어 failed 로 끝낸 검토")
INTAKE_SWEEP_RECOVERED = Counter("review_intake_sweep_recovered", "intake sweep 이 다시 처리한 processing intake")
INTAKE_SWEEP_FAILED = Counter("review_intake_sweep_failed", "처리 시도 3번을 넘어 failed(RETRY_EXHAUSTED)로 끝낸 intake")

UNTRACKED_ROUTES = frozenset({"/metrics", "/healthz", "/readyz"})
WEBHOOK_RESULTS = ("started", "intake", "skipped", "duplicate", "rejected", "error")
DB_STATS_TTL = 15.0
DB_STATS_WINDOW = timedelta(days=1)


def safe(fn: Any, *args: Any) -> None:
    """지표 기록 — 실패해도 삼킨다."""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        log.warning("지표 기록 실패", exc_info=True)


def observe_http(route: str, method: str, status: int, seconds: float) -> None:
    if route in UNTRACKED_ROUTES:
        return
    safe(lambda: HTTP_REQUESTS.labels(route, method, str(status)).inc())
    safe(lambda: HTTP_DURATION.labels(route, method).observe(seconds))


def webhook(event: str, result: str) -> None:
    safe(lambda: WEBHOOK_EVENTS.labels(event, result).inc())


def pull_request_result(body: dict[str, Any]) -> str:
    """on_pull_request 응답 → started·intake·skipped·duplicate."""
    if "kind" in body:
        return "intake"
    if body.get("skipped") in ("already reviewed", "already taken"):
        return "duplicate"
    if "review_id" in body and "skipped" not in body:
        return "started"
    return "skipped"  # 포크·base·action·닫힌 PR·deploy.yaml 없음 등


def argocd_result(body: dict[str, Any]) -> str:
    if body.get("duplicate"):
        return "duplicate"
    if "ignored" in body:
        return "skipped"
    return "started"


class DbStats(Collector):
    """업무 DB 게이지. refresh() 를 /metrics 핸들러가 부르고(비동기), collect() 는 캐시만 읽는다."""

    def __init__(self) -> None:
        self.counts: list[dict[str, Any]] = []
        self.oldest: list[dict[str, Any]] = []
        self.refreshed_at = float("-inf")

    async def refresh(self, repo: Any, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if now - self.refreshed_at < DB_STATS_TTL:
            return
        try:
            counts = await repo.status_counts(DB_STATS_WINDOW)
            oldest = await repo.needs_human_oldest()
        except Exception:  # noqa: BLE001 — 마지막 값을 그대로 낸다
            log.warning("지표용 DB 조회 실패 — 마지막 값을 쓴다", exc_info=True)
            return
        self.counts, self.oldest, self.refreshed_at = counts, oldest, now

    def collect(self) -> Iterator[GaugeMetricFamily]:
        reviews = GaugeMetricFamily("review_reviews", "최근 1일 안에 바뀐 검토 수 (상태별)", labels=["app", "env", "status"])
        for row in self.counts:
            reviews.add_metric([row["app"], row["target_env"], row["status"]], float(row["count"]))
        yield reviews
        waiting = GaugeMetricFamily("review_needs_human_oldest_seconds", "가장 오래 기다린 needs_human 검토의 대기 시간",
                                    labels=["app", "env"])
        for row in self.oldest:
            waiting.add_metric([row["app"], row["target_env"]], float(row["seconds"]))
        yield waiting


DB_STATS = DbStats()
REGISTRY.register(DB_STATS)


async def render(repo: Any) -> tuple[bytes, str]:
    await DB_STATS.refresh(repo)
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
