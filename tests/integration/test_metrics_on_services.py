"""지표 — 실제 Postgres 의 게이지 쿼리, 실제 Kafka consumer 의 lag 계산."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from psycopg.conninfo import make_conninfo

from review_common.migrate import migrate
from review_common.repository import PostgresReviewRepository, make_pool
from review_worker.metrics import update_lag

DSN = os.environ.get("REVIEW_IT_DSN")
BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP")
pytestmark = pytest.mark.skipif(not (DSN and BOOTSTRAP), reason="REVIEW_IT_DSN·KAFKA_BOOTSTRAP 필요")
REPO = "Crystal-SBHackathon2026/sample-app"


@pytest.fixture
async def pool() -> AsyncIterator[Any]:
    name = f"review_it_{uuid.uuid4().hex[:8]}"
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{name}"')
    conninfo = make_conninfo(DSN, dbname=name)
    await migrate(conninfo)
    p = make_pool(conninfo)
    await p.open(wait=True)
    yield p
    await p.close()
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as admin:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


async def test_gauge_queries_on_postgres(pool: Any) -> None:
    from datetime import timedelta

    repo = PostgresReviewRepository(pool)
    for i, status in enumerate(["needs_human", "needs_human", "committed", "received"]):
        sha = f"{i}" * 40
        await repo.insert_review(review_id=f"rv_{i}", app="sample-app", target_env="aws", repo_id=REPO,
                                 spec_ref={"repository": REPO, "commit": sha, "path": "deploy.yaml"},
                                 pr_head_sha=sha, requested_by="it")
        await repo.update_review(f"rv_{i}", status=status)
    async with pool.connection() as conn:
        await conn.execute("UPDATE reviews SET updated_at = now() - interval '2 hours' WHERE review_id = 'rv_0'")
        await conn.execute("UPDATE reviews SET updated_at = now() - interval '2 days' WHERE review_id = 'rv_3'")

    counts = {(r["app"], r["target_env"], r["status"]): r["count"] for r in await repo.status_counts(timedelta(days=1))}
    [oldest] = await repo.needs_human_oldest()

    assert counts == {("sample-app", "aws", "needs_human"): 2, ("sample-app", "aws", "committed"): 1}  # 2일 전 것은 빠진다
    assert (oldest["app"], oldest["target_env"]) == ("sample-app", "aws")
    assert 7190 < oldest["seconds"] < 7300


async def test_consumer_lag_on_kafka() -> None:
    topic = f"it-lag-{uuid.uuid4().hex[:6]}"
    admin = AIOKafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    await admin.start()
    await admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
    await admin.close()
    producer = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP)
    await producer.start()
    for i in range(5):
        await producer.send_and_wait(topic, f"m{i}".encode())
    await producer.stop()

    consumer = AIOKafkaConsumer(topic, bootstrap_servers=BOOTSTRAP, group_id=f"it-{uuid.uuid4().hex[:6]}",
                                enable_auto_commit=False, auto_offset_reset="earliest", max_poll_records=1)
    await consumer.start()
    try:
        while not consumer.assignment():
            await consumer.getmany(timeout_ms=200)
        assert await update_lag(consumer) == {(topic, 0): 5}  # 아직 커밋 없음 — 시작 오프셋부터

        got = 0
        while got < 2:
            for records in (await consumer.getmany(timeout_ms=500, max_records=1)).values():
                got += len(records)
                await consumer.commit()
        assert await update_lag(consumer) == {(topic, 0): 3}
    finally:
        await consumer.stop()
