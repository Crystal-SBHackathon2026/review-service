"""Run with a socket-only disposable PostgreSQL, never a deployed database."""
import asyncio
import os
import uuid

from langgraph.checkpoint.memory import InMemorySaver
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
import pytest_asyncio

from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.retrieval.file_retriever import FileRetriever
from review_common.deployment_requests import RequestBusy, activate_multitarget
from review_common.migrate import migrate
from review_common.repository import PostgresReviewRepository, make_pool
from review_worker.graph import Deps, build_graph
from review_worker.handler import ReviewHandler
from tests.test_deployment_requests import System, HEAD, MERGE, REPO

DSN = os.environ.get("MULTITARGET_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="disposable MULTITARGET_TEST_DSN is required")


@pytest_asyncio.fixture
async def database():
    schema = "request_test_" + uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(DSN, options=f"-csearch_path={schema}")
    try:
        assert len(await migrate(scoped)) == 13
        assert await migrate(scoped) == []
        pool = make_pool(scoped)
        await pool.open(wait=True)
        # The old version's conflict predicate must still work before explicit activation.
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO reviews(review_id,app,target_env,repo_id,spec_ref,pr_head_sha,requested_by) "
                               "VALUES ('old','sample-app','aws','old/repo','{}','oldsha','tester') "
                               "ON CONFLICT (repo_id,pr_head_sha) WHERE status NOT IN ('failed','superseded') DO NOTHING")
        await activate_multitarget(pool)
        try:
            yield PostgresReviewRepository(pool)
        finally:
            await pool.close()
    finally:
        async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.asyncio
async def test_real_db_identity_and_concurrent_insert(database):
    s = System()
    s.repo = database
    ids = await asyncio.gather(s.start(), s.start())
    assert ids[0] == ids[1]
    children = await database.request_children(ids[0])
    assert len(children) == 3
    assert {c["target_env"] for c in children} == {"aws", "gcp", "local"}
    kwargs = dict(app="sample-app", target_env="aws", repo_id=REPO, spec_ref=dict(repository=REPO, commit=HEAD),
                  pr_head_sha=HEAD, requested_by="user")
    a = await database.insert_review(review_id="legacy_a", **kwargs)
    b = await database.insert_review(review_id="legacy_b", **kwargs)
    assert a == b == "legacy_a"
    assert len(await database.request_children(ids[0])) == 3


@pytest.mark.asyncio
async def test_real_db_lock_contention_releases_on_exception(database):
    async with database.request_lock(REPO, 7):
        with pytest.raises(RequestBusy):
            async with database.request_lock(REPO, 7):
                pytest.fail("second coordinator entered the same PR")
    with pytest.raises(RuntimeError):
        async with database.request_lock(REPO, 7):
            raise RuntimeError("worker stopped")
    async with database.request_lock(REPO, 7):
        assert True


@pytest.mark.asyncio
async def test_full_review_to_release_using_postgres(database):
    s = System()
    s.repo = database
    s.coordinator.repo = database
    graph = build_graph(Deps(repo=database, github=s.gh, publisher=s.publisher,
                             llm=ScriptedLLM(oracle_review), retriever=FileRetriever()), InMemorySaver())
    s.handler = ReviewHandler(database, graph)
    rid = await s.start()
    await s.work()
    await s.coordinator.advance(rid)
    assert s.gh.merges == [HEAD]
    assert (await database.get_request(rid))["state"] == "committed"
    children = await database.request_children(rid)
    assert {c["merge_sha"] for c in children} == {MERGE}
    assert {c["status"] for c in children} == {"committed"}
    assert await database.find_by_merge_sha(app="sample-app", target_env=None, image_tag=MERGE) is None
    await s.coordinator.advance(rid)
    assert len(s.gh.merges) == 1


@pytest.mark.asyncio
async def test_real_db_retry_dedup_and_durable_result(database):
    s = System(("gcp",))
    s.repo = database
    rid = await s.start()
    a, b = await asyncio.gather(database.insert_request_retry("retry_test", rid, "gcp"),
                                database.insert_request_retry("retry_test", rid, "gcp"))
    assert a["retry_id"] == b["retry_id"]
    assert len(await database.pending_request_retries()) == 1
    await database.finish_request_retry("retry_test", sha="e" * 40)
    assert await database.pending_request_retries() == []
    stored = await database.insert_request_retry("retry_test", rid, "gcp")
    assert stored["state"] == "committed" and stored["gitops_commit_sha"] == "e" * 40
