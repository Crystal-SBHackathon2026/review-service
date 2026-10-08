"""워커 진입점 — python -m review_worker.main

Kafka consumer(그룹 review-worker)가 review.requested·review.resumed 를 한 건씩 처리하고 오프셋을 커밋한다.
DB 연결 같은 처리 밖 예외는 커밋하지 않고 프로세스를 내린다 — 재시작 뒤 같은 메시지를 다시 받는다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from review_ai.judge.llm import CachedLLM, ClaudeLLM, LlmUnavailable
from review_ai.messages import TOPIC as REQUESTED_TOPIC
from review_ai.retrieval.file_retriever import FileRetriever
from review_common.github import GitHubClient
from review_common.migrate import migrate
from review_common.repository import PostgresReviewRepository, make_pool
from review_common.resumed import TOPIC as RESUMED_TOPIC
from review_common.settings import db_conninfo, kafka_bootstrap
from review_worker.graph import Deps, build_graph
from review_worker.handler import ReviewHandler

log = logging.getLogger("review_worker")
GROUP_ID = "review-worker"


def make_llm() -> CachedLLM | None:
    try:
        return CachedLLM(ClaudeLLM())
    except LlmUnavailable as exc:
        log.warning("LLM 없이 시작 — 판단이 필요한 검토는 LLM_UNAVAILABLE 로 사람에게 간다: %s", exc)
        return None


async def run() -> None:
    conninfo = db_conninfo()
    await migrate(conninfo)
    pool = make_pool(conninfo)
    await pool.open(wait=True)
    github = GitHubClient()
    consumer = AIOKafkaConsumer(
        REQUESTED_TOPIC, RESUMED_TOPIC,
        bootstrap_servers=kafka_bootstrap(),
        group_id=GROUP_ID,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_records=1,
        max_poll_interval_ms=int(os.environ.get("KAFKA_MAX_POLL_INTERVAL_MS", "900000")),  # judge 재시도까지 기다린다
    )
    producer = AIOKafkaProducer(bootstrap_servers=kafka_bootstrap(), acks="all", enable_idempotence=True)

    class KafkaPublisher:
        async def send(self, topic: str, key: str, value: bytes) -> None:
            await producer.send_and_wait(topic, value=value, key=key.encode())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        repo = PostgresReviewRepository(pool)
        await producer.start()
        deps = Deps(repo=repo, github=github, publisher=KafkaPublisher(), llm=make_llm(), retriever=FileRetriever(),
                    ci_app_slug=os.environ.get("GITHUB_CI_APP_SLUG", "github-actions") or None)
        graph = build_graph(deps, checkpointer)
        handler = ReviewHandler(repo, graph)
        await consumer.start()
        log.info("review-worker 시작: %s", consumer.subscription())
        while not stop.is_set():
            batch = await consumer.getmany(timeout_ms=1000)
            for records in batch.values():
                for record in records:
                    await handler.handle(record.topic, record.value)
                    await consumer.commit()
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
