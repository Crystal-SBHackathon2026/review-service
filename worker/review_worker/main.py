"""워커 진입점 — python -m review_worker.main

Kafka consumer(그룹 review-worker)가 review.requested·review.resumed 를 한 건씩 처리하고 오프셋을 커밋한다.
DB 연결 같은 처리 밖 예외는 커밋하지 않고 프로세스를 내린다 — 재시작 뒤 같은 메시지를 다시 받는다.

livenessProbe: 메인 루프가 poll 할 때마다(메시지가 없어도 1초마다) WORKER_ALIVE_FILE(/tmp/worker-alive) 시각을 갱신한다.
메시지 하나를 처리하는 동안(judge 재시도로 몇 분)에도 ALIVE_TOUCH_SECONDS 마다 갱신하되, Kafka 가 consumer 를 그룹에서
빼는 max_poll_interval 을 넘으면 멈춘다 — 그 뒤엔 probe(파일이 2분 넘게 그대로)가 파드를 재시작한다.

지표: METRICS_PORT(기본 9100) 의 /metrics (review_worker.metrics). consumer lag 는 30초마다 워커 안에서 계산한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Protocol

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from prometheus_client import start_http_server

from review_ai.judge.llm import CachedLLM, ClaudeLLM, LlmUnavailable
from review_ai.messages import TOPIC as REQUESTED_TOPIC
from review_ai.retrieval.case_retriever import CaseRetriever
from review_ai.retrieval.runtime import make_retriever, qdrant_from_url
from review_common.github import GitHubClient, GitHubGitClient
from review_common.kafka import KafkaPublisher
from review_common.migrate import migrate
from review_common.repository import PostgresReviewRepository, make_pool
from review_common.resumed import TOPIC as RESUMED_TOPIC
from review_common.settings import db_conninfo, kafka_bootstrap
from review_worker.commit_overlay import make_commit_overlay, make_overlay_guard
from review_worker.graph import Deps, build_graph
from review_worker.handler import ReviewHandler
from review_worker.metrics import DEFAULT_METRICS_PORT, LAST_HEARTBEAT, MeteredLLM, report_lag, safe

log = logging.getLogger("review_worker")
GROUP_ID = "review-worker"
# SDK 기본값(10분·재시도 2번)이면 그래프 재시도(JUDGE_MAX_RETRIES)와 곱해져 한 검토가 judge 에서 최대 12×10분 멈춘다.
# 재시도는 그래프 한 곳에서만 한다. 120초면 보통 판단 출력(수천 토큰)은 끝난다
JUDGE_CLIENT_OPTIONS: dict[str, Any] = {"timeout": 120.0, "max_retries": 0}
ALIVE_FILE = "/tmp/worker-alive"
ALIVE_TOUCH_SECONDS = 30.0  # 처리 중 갱신 주기 — probe 기준(2분)보다 넉넉히 짧게


def make_llm() -> CachedLLM | None:
    try:
        return CachedLLM(MeteredLLM(ClaudeLLM(client_options=JUDGE_CLIENT_OPTIONS), purpose="judge"))
    except LlmUnavailable as exc:
        log.warning("LLM 없이 시작 — 판단이 필요한 검토는 LLM_UNAVAILABLE 로 사람에게 간다: %s", exc)
        return None


class AliveFile:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def touch(self) -> None:
        try:
            self.path.touch()
        except OSError as exc:  # 파일 하나 못 써서 검토를 멈추지 않는다 — probe 가 재시작한다
            log.warning("alive 파일 %s 갱신 실패: %s", self.path, exc)
            return
        safe(lambda: LAST_HEARTBEAT.set(time.time()))


async def keep_alive_while(alive: AliveFile, work: Awaitable[Any], limit_seconds: float) -> Any:
    """work 를 기다리며 ALIVE_TOUCH_SECONDS 마다 alive 를 갱신한다. limit_seconds 가 지나면 갱신을 멈춘다."""
    loop = asyncio.get_running_loop()
    task, started = asyncio.ensure_future(work), loop.time()
    while True:
        done, _ = await asyncio.wait({task}, timeout=ALIVE_TOUCH_SECONDS)
        if done:
            return task.result()
        if loop.time() - started < limit_seconds:
            alive.touch()


class Consumer(Protocol):
    async def getmany(self, *, timeout_ms: int) -> dict[Any, list[Any]]: ...

    async def commit(self) -> None: ...


async def consume(consumer: Consumer, handler: ReviewHandler, stop: asyncio.Event, alive: AliveFile,
                  handle_limit_seconds: float) -> None:
    """메인 루프. poll 마다 alive 를 갱신하고, 메시지를 하나씩 처리한 뒤 오프셋을 커밋한다."""
    while not stop.is_set():
        alive.touch()
        batch = await consumer.getmany(timeout_ms=1000)
        for records in batch.values():
            for record in records:
                await keep_alive_while(alive, handler.handle(record.topic, record.value), handle_limit_seconds)
                await consumer.commit()


async def run() -> None:
    start_http_server(int(os.environ.get("METRICS_PORT") or DEFAULT_METRICS_PORT))
    conninfo = db_conninfo()
    await migrate(conninfo)
    pool = make_pool(conninfo)
    await pool.open(wait=True)
    github = GitHubClient()
    max_poll_interval_ms = int(os.environ.get("KAFKA_MAX_POLL_INTERVAL_MS", "900000"))  # judge 재시도까지 기다린다
    consumer = AIOKafkaConsumer(
        REQUESTED_TOPIC, RESUMED_TOPIC,
        bootstrap_servers=kafka_bootstrap(),
        group_id=GROUP_ID,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_records=1,
        max_poll_interval_ms=max_poll_interval_ms,
    )
    producer = AIOKafkaProducer(bootstrap_servers=kafka_bootstrap(), acks="all", enable_idempotence=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        repo = PostgresReviewRepository(pool)
        await producer.start()
        # 규칙 문서(파일) → Qdrant 의미 검색(QDRANT_URL 이 있을 때, 실패하면 건너뜀) → 판단 사례
        retriever = make_retriever(CaseRetriever(repo), qdrant=qdrant_from_url(os.environ.get("QDRANT_URL")))
        gitops = GitHubGitClient(github)  # gitops 레포: GITOPS_REPO
        deps = Deps(repo=repo, github=github, publisher=KafkaPublisher(producer), llm=make_llm(),
                    retriever=retriever, commit_overlay=make_commit_overlay(gitops),
                    overlay_guard=make_overlay_guard(gitops),
                    ci_app_slug=os.environ.get("GITHUB_CI_APP_SLUG", "github-actions") or None,
                    public_url=os.environ.get("REVIEW_API_PUBLIC_URL") or None)
        graph = build_graph(deps, checkpointer)
        handler = ReviewHandler(repo, graph, files=github)
        await consumer.start()
        log.info("review-worker 시작: %s", consumer.subscription())
        alive = AliveFile(os.environ.get("WORKER_ALIVE_FILE") or ALIVE_FILE)
        lag = asyncio.create_task(report_lag(consumer))
        try:
            await consume(consumer, handler, stop, alive, max_poll_interval_ms / 1000)
        finally:
            lag.cancel()
    finally:
        await consumer.stop()
        await producer.stop()
        await github.aclose()
        await pool.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
