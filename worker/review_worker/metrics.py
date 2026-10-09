"""워커 지표 — HTTP 서버가 없으니 prometheus_client.start_http_server(METRICS_PORT, 기본 9100) 로 /metrics 를 연다.

라벨은 값 종류가 적은 것만 (topic·kind·node·app·env·verdict·status·reason·purpose). review_id·commit_sha 는 넣지 않는다.
지표를 기록하다 실패해도 검토 처리에는 영향이 없다 (safe).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.errors import GraphBubbleUp
from prometheus_client import Counter, Gauge, Histogram

log = logging.getLogger(__name__)

MESSAGES = Counter("review_worker_messages", "처리한 Kafka 메시지", ["topic", "kind", "result"])
NODE_DURATION = Histogram("review_worker_node_duration_seconds", "그래프 노드 처리 시간", ["node"],
                          buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600))
NODE_ERRORS = Counter("review_worker_node_errors", "그래프 노드 예외", ["node"])
VERDICTS = Counter("review_worker_verdicts", "판정 (재검사 회차마다)", ["app", "env", "verdict"])
NEEDS_HUMAN_REASONS = Counter("review_worker_needs_human_reasons", "needs_human 사유 코드", ["reason"])
FINISHED = Counter("review_worker_finished", "워커가 끝낸 검토의 최종 상태", ["app", "env", "status"])
LLM_CALLS = Counter("review_worker_llm_calls", "LLM 호출 (캐시 적중 제외)", ["purpose", "result"])
LLM_DURATION = Histogram("review_worker_llm_duration_seconds", "LLM 호출 시간", ["purpose"],
                         buckets=(1, 2.5, 5, 10, 20, 30, 60, 90, 120, 180))
LLM_TOKENS = Counter("review_worker_llm_tokens", "LLM 사용 토큰", ["purpose", "type"])
CONSUMER_LAG = Gauge("review_worker_consumer_lag", "Kafka consumer lag (끝 오프셋 - 커밋 오프셋)", ["topic", "partition"])
LAST_HEARTBEAT = Gauge("review_worker_last_heartbeat_timestamp", "생존 파일을 마지막으로 갱신한 시각 (unix 초)")

DEFAULT_METRICS_PORT = 9100
LAG_EVERY_SECONDS = 30.0


def safe(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001
        log.warning("지표 기록 실패", exc_info=True)


def app_env(state: dict[str, Any]) -> tuple[str, str]:
    spec = state.get("deploy_spec") or {}
    return str((spec.get("metadata") or {}).get("name") or "unknown"), str(state.get("target_env") or "unknown")


def finished(app: str, env: str, status: str) -> None:
    safe(lambda: FINISHED.labels(app, env, status).inc())


def timed_node(name: str, node: Callable[[dict[str, Any]], Awaitable[Any]]) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    """노드 처리 시간·예외를 기록한다. interrupt(GraphBubbleUp)는 대기라 시간도 오류도 남기지 않는다."""
    async def timed(state: dict[str, Any]) -> Any:
        started = time.perf_counter()
        try:
            result = await node(state)
        except GraphBubbleUp:
            raise
        except Exception:
            safe(lambda: NODE_ERRORS.labels(name).inc())
            raise
        safe(lambda: NODE_DURATION.labels(name).observe(time.perf_counter() - started))
        return result

    timed.__name__ = name
    return timed


class MeteredLLM:
    """LlmClient 를 감싸 호출 수·시간·토큰을 남긴다. CachedLLM 안쪽에 두어 캐시 적중은 세지 않는다."""

    def __init__(self, inner: Any, purpose: str) -> None:
        self.inner = inner
        self.purpose = purpose
        self.model = inner.model

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def complete(self, request: Any) -> Any:
        started = time.perf_counter()
        try:
            response = await self.inner.complete(request)
        except Exception as exc:
            result = "timeout" if _is_timeout(exc) else "error"
            safe(lambda: LLM_CALLS.labels(self.purpose, result).inc())
            raise
        finally:
            safe(lambda: LLM_DURATION.labels(self.purpose).observe(time.perf_counter() - started))
        safe(lambda: LLM_CALLS.labels(self.purpose, "ok").inc())
        usage = getattr(response, "usage", None) or {}
        for kind in ("input", "output"):
            tokens = usage.get(f"{kind}_tokens")
            if isinstance(tokens, int) and tokens > 0:
                safe(lambda kind=kind, tokens=tokens: LLM_TOKENS.labels(self.purpose, kind).inc(tokens))
        return response


def _is_timeout(exc: BaseException) -> bool:
    """ClaudeLLM 은 APITimeoutError 를 TransientError 로 바꿔 올린다 — 원인까지 본다."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, TimeoutError) or "Timeout" in type(seen).__name__ or "Timeout" in str(seen):
            return True
        seen = seen.__cause__
    return False


async def update_lag(consumer: Any) -> dict[tuple[str, int], int]:
    """할당된 파티션마다 끝 오프셋 - 커밋 오프셋. 커밋이 없으면 시작 오프셋부터 센다."""
    partitions = list(consumer.assignment())
    if not partitions:
        CONSUMER_LAG.clear()
        return {}
    end = await consumer.end_offsets(partitions)
    begin = None
    lags: dict[tuple[str, int], int] = {}
    for tp in partitions:
        committed = await consumer.committed(tp)
        if committed is None:
            begin = begin or await consumer.beginning_offsets(partitions)
            committed = begin[tp]
        lags[(tp.topic, tp.partition)] = max(end[tp] - committed, 0)
    CONSUMER_LAG.clear()  # 재할당으로 빠진 파티션은 지운다
    for (topic, partition), lag in lags.items():
        CONSUMER_LAG.labels(topic, str(partition)).set(lag)
    return lags


async def report_lag(consumer: Any, every: float = LAG_EVERY_SECONDS) -> None:
    """워커가 떠 있는 동안 lag 를 주기적으로 계산한다. 별도 exporter 파드 없이."""
    while True:
        try:
            await update_lag(consumer)
        except Exception:  # noqa: BLE001
            log.warning("consumer lag 계산 실패", exc_info=True)
        await asyncio.sleep(every)
