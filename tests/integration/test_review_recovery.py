"""멈춘 검토 회수 (P1-4) — 연재님 재현 4개가 review sweep 으로 회수돼 끝까지 간다. 실제 Postgres·체크포인터.

    REVIEW_IT_DSN=postgresql://review:review@localhost:5432/oneaction_review KAFKA_BOOTSTRAP=localhost:9092 \
        .venv/bin/python -m pytest -q tests/integration/test_review_recovery.py

Kafka 대신 Bus 가 발행한 메시지를 모았다가 워커 handler 에 넣는다. GitHub 은 가짜다.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo

from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.retrieval.file_retriever import FileRetriever
from review_api.app import ApiDeps, create_app
from review_api.recovery import MAX_RECOVERIES, recover_stale_reviews
from review_common.github import GitHubError, SpecNotFound
from review_common.migrate import migrate
from review_common.repository import PostgresReviewRepository, make_pool
from review_worker.graph import Deps, build_graph
from review_worker.handler import ReviewHandler

DSN = os.environ.get("REVIEW_IT_DSN")
BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP")
pytestmark = pytest.mark.skipif(not (DSN and BOOTSTRAP), reason="REVIEW_IT_DSN·KAFKA_BOOTSTRAP 필요")

SAMPLES = Path(__file__).resolve().parents[2] / "ai" / "samples"
REPO = "Crystal-SBHackathon2026/sample-app"
FIX_REPO = "Crystal-SBHackathon2026/sample-orders"  # 샘플 05 의 레포
HEAD = "a" * 40
FIX_SHA = "b" * 40
MERGE_SHA = "c0ffee1" + "0" * 33
GITOPS_SHA = "d" * 40
SECRET = "it-secret"
SUCCESS = [{"status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}]


@pytest.fixture
async def pool() -> AsyncIterator[Any]:
    name = f"review_it_{uuid.uuid4().hex[:8]}"
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{name}"')
    conninfo = make_conninfo(DSN, dbname=name)
    await migrate(conninfo)
    p = make_pool(conninfo, max_size=8)
    await p.open(wait=True)
    yield p
    await p.close()
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as admin:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


class FakeGitHub:
    """앱 레포 — 커밋별 deploy.yaml, PR(병합하면 닫힌다), check suite, 브랜치 이동."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.pulls: dict[str, dict[str, Any]] = {}  # head sha → PR
        self.prepared: dict[str, str] = {}
        self.suites: list[dict[str, Any]] = []
        self.merged: list[str] = []
        self.statuses: list[tuple[str, str]] = []

    def open_pr(self, repository: str, sha: str, number: int = 3) -> None:
        self.pulls[sha] = {"number": number, "state": "open", "merged_at": None, "merge_commit_sha": None,
                           "head": {"sha": sha, "ref": "feature", "repo": {"full_name": repository}}}

    async def get_file(self, repository: str, path: str, ref: str, **_: Any) -> str:
        if path != "deploy.yaml" or ref not in self.files:
            raise SpecNotFound("없음", 404)
        return self.files[ref]

    async def check_suites(self, repository: str, sha: str) -> list[dict[str, Any]]:
        return self.suites

    async def pulls_for_commit(self, repository: str, sha: str) -> list[dict[str, Any]]:
        return [self.pulls[sha]] if sha in self.pulls else []

    async def prepare_file_commit(self, repository: str, *, parent: str, path: str, content: str,
                                  message: str) -> str:
        self.prepared[FIX_SHA] = content
        return FIX_SHA

    async def update_branch(self, repository: str, branch: str, sha: str) -> None:
        self.files[sha] = self.prepared.pop(sha)
        for old, pull in list(self.pulls.items()):
            if pull["state"] == "open":
                pull["head"]["sha"] = sha
                self.pulls[sha] = self.pulls.pop(old)

    async def merge_pull(self, repository: str, number: int, *, head_sha: str) -> str:
        pull = self.pulls[head_sha]
        if pull["state"] != "open":
            raise GitHubError("이미 닫혔다", 405)
        pull.update(state="closed", merged_at=datetime.now(UTC).isoformat(), merge_commit_sha=MERGE_SHA)
        self.merged.append(head_sha)
        return MERGE_SHA

    async def create_commit_status(self, repository: str, sha: str, *, state: str, context: str, description: str,
                                   target_url: str | None = None) -> None:
        self.statuses.append((sha, state))


class Bus:
    """Kafka 대신 — 발행한 메시지를 모은다. fail 이 남아 있으면 그 횟수만큼 발행이 실패한다."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bytes]] = []
        self.fail = 0

    async def send(self, topic: str, key: str, value: bytes) -> None:
        if self.fail:
            self.fail -= 1
            raise ConnectionError("Kafka 발행 실패")
        self.sent.append((topic, key, value))

    async def deliver(self, handler: ReviewHandler) -> None:
        pending, self.sent = self.sent, []
        for topic, _, value in pending:
            await handler.handle(topic, value)


class Stack:
    def __init__(self, pool: Any, commit_overlay: Any = None) -> None:
        self.pool = pool
        self.repo = PostgresReviewRepository(pool)
        self.github = FakeGitHub()
        self.bus = Bus()
        self.overlay_calls = 0
        self.overlay_failures = 0

        async def overlay(state: dict[str, Any]) -> dict[str, Any]:
            self.overlay_calls += 1
            if self.overlay_failures:
                self.overlay_failures -= 1
                raise RuntimeError("gitops push 실패")
            return {"deploy_result": {"status": "committed", "commit_sha": GITOPS_SHA, "reason": None}}

        self.worker_deps = Deps(repo=self.repo, github=self.github, publisher=self.bus,
                                llm=ScriptedLLM(oracle_review), retriever=FileRetriever(), commit_overlay=overlay,
                                retry_backoff_seconds=0)
        self.api_deps = ApiDeps(repo=self.repo, specs=self.github, publisher=self.bus, github=self.github,  # type: ignore[arg-type]
                                github_webhook_secret=SECRET, api_token="t", argocd_webhook_token="t")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.api_deps)),
                                        base_url="http://api")

    async def start(self) -> None:
        saver = AsyncPostgresSaver(self.pool)
        await saver.setup()
        self.handler = ReviewHandler(self.repo, build_graph(self.worker_deps, saver))

    async def webhook(self, event: str, body: dict[str, Any]) -> httpx.Response:
        raw = json.dumps(body).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        return await self.client.post("/webhooks/github", content=raw,
                                      headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sig})

    async def pr(self, action: str, sha: str = HEAD, repository: str = REPO) -> httpx.Response:
        return await self.webhook("pull_request", {
            "action": action, "number": 3, "sender": {"login": "octo-dev"},
            "repository": {"full_name": repository, "default_branch": "main"},
            "pull_request": {"number": 3, "base": {"ref": "main", "repo": {"full_name": repository}},
                             "head": {"sha": sha, "ref": "feature", "repo": {"full_name": repository}}}})

    async def ci_done(self, sha: str = HEAD) -> None:
        self.github.suites = SUCCESS
        await self.webhook("check_suite", {"action": "completed", "check_suite": {
            "head_sha": sha, "conclusion": "success", "app": {"slug": "github-actions"}}})
        await self.bus.deliver(self.handler)

    async def age(self, review_id: str, minutes: int = 11) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE reviews SET updated_at = now() - make_interval(mins => %s)"
                               " WHERE review_id = %s", (minutes, review_id))

    async def sweep(self) -> list[str]:
        return [r["action"] for r in await recover_stale_reviews(self.api_deps)]

    async def row(self, review_id: str) -> dict[str, Any]:
        return await self.repo.get_review(review_id)  # type: ignore[return-value]


@pytest.fixture
async def stack(pool: Any) -> AsyncIterator[Stack]:
    s = Stack(pool)
    await s.start()
    s.github.files[HEAD] = (SAMPLES / "01-pass-sample-app-aws.yaml").read_text(encoding="utf-8")
    s.github.open_pr(REPO, HEAD)
    yield s
    await s.client.aclose()


async def test_db_error_after_claim_strands_review(stack: Stack) -> None:
    """① claim 뒤 DB 오류 — 워커가 내려가고 재전송은 claim 에 실패해 reviewing 에 고정됐다 → sweep 이 회수해 끝까지 간다."""
    rid = (await stack.pr("opened")).json()["review_id"]
    original = stack.repo.get_baseline

    async def db_down(*_: Any) -> Any:
        raise psycopg.OperationalError("DB 연결 끊김")

    stack.repo.get_baseline = db_down  # type: ignore[method-assign]
    [message] = stack.bus.sent
    with pytest.raises(psycopg.OperationalError):
        await stack.bus.deliver(stack.handler)  # 오프셋을 커밋하지 않고 프로세스가 내려간다
    stack.repo.get_baseline = original  # type: ignore[method-assign]
    stack.bus.sent = [message]
    await stack.bus.deliver(stack.handler)  # 재전송 — claim 실패로 건너뛴다 (예전에는 여기서 끝)
    assert (await stack.row(rid))["status"] == "reviewing"

    assert await stack.sweep() == []  # 아직 REVIEW_STALE_AFTER 전
    await stack.age(rid)
    assert await stack.sweep() == ["requested"]
    assert (await stack.row(rid))["status"] == "received"
    await stack.bus.deliver(stack.handler)
    assert (await stack.row(rid))["status"] == "waiting_ci"

    await stack.ci_done()
    row = await stack.row(rid)
    assert (row["status"], row["gitops_commit_sha"], row["recover_count"]) == ("committed", GITOPS_SHA, 1)


async def test_publish_failure_then_redelivery_is_skipped_forever(stack: Stack) -> None:
    """② 웹훅 발행 실패(503) — failed 행 때문에 같은 SHA 재전송이 영원히 skip 됐다 → 새 검토를 만든다."""
    stack.bus.fail = 1
    assert (await stack.pr("opened")).status_code == 503
    [failed] = await stack.repo.list_reviews(["failed"], 10)

    resp = await stack.pr("opened")  # GitHub 재전송

    assert resp.status_code == 202, resp.text
    rid = resp.json()["review_id"]
    assert rid != failed["review_id"]
    await stack.bus.deliver(stack.handler)
    await stack.ci_done()
    assert (await stack.row(rid))["status"] == "committed"
    assert (await stack.row(failed["review_id"]))["status"] == "failed"


async def test_merged_but_overlay_failed(stack: Stack) -> None:
    """③ 병합 뒤 gitops 커밋 실패 — merge_sha 만 있고 배포가 안 된 채 failed 였다 → commit_overlay 만 다시 해 committed."""
    stack.overlay_failures = 1
    rid = (await stack.pr("opened")).json()["review_id"]
    await stack.bus.deliver(stack.handler)
    await stack.ci_done()
    row = await stack.row(rid)
    assert (row["status"], row["merge_sha"], row["gitops_commit_sha"]) == ("merging", MERGE_SHA, None)

    await stack.age(rid)
    assert await stack.sweep() == ["retry_overlay"]
    await stack.bus.deliver(stack.handler)

    row = await stack.row(rid)
    assert (row["status"], row["merge_sha"], row["gitops_commit_sha"]) == ("committed", MERGE_SHA, GITOPS_SHA)
    assert row["created_at"] <= row["judged_at"] <= row["merged_at"] <= row["gitops_committed_at"]
    assert stack.github.merged == [HEAD]  # 다시 병합하지 않는다
    assert stack.overlay_calls == 2


async def test_commit_fix_publish_failure_strands_new_review(stack: Stack) -> None:
    """④ commit_fix 뒤 발행 실패 — 브랜치는 옮겼는데 새 검토가 received 에 고정, synchronize 는 그 행 때문에 skip 됐다.

    → sweep 이 수정 커밋의 명세로 review.requested 를 다시 만들어 끝까지 간다.
    """
    stack.github.files = {HEAD: (SAMPLES / "05-fix-engine-unsupported-local.yaml").read_text(encoding="utf-8")}
    stack.github.pulls = {}
    stack.github.open_pr(FIX_REPO, HEAD)
    old = (await stack.pr("opened", repository=FIX_REPO)).json()["review_id"]
    stack.bus.fail = 1  # 워커가 수정 커밋 재검토를 발행할 때
    await stack.bus.deliver(stack.handler)

    old_row = await stack.row(old)
    new = old_row["superseded_by"]
    assert old_row["status"] == "superseded"
    assert ((await stack.row(new))["status"], stack.bus.sent) == ("received", [])
    resp = await stack.pr("synchronize", sha=FIX_SHA, repository=FIX_REPO)  # 브랜치가 움직여 온 웹훅
    assert resp.json() == {"skipped": "already reviewed", "review_id": new}

    await stack.age(new)
    assert await stack.sweep() == ["requested"]
    [(_, _, value)] = stack.bus.sent
    assert json.loads(value)["autofix_commit"] is True
    await stack.bus.deliver(stack.handler)
    assert (await stack.row(new))["status"] == "waiting_ci"
    await stack.ci_done(FIX_SHA)
    assert (await stack.row(new))["status"] == "committed"
    assert stack.github.merged == [FIX_SHA]


async def test_recovered_more_than_max_times_fails(stack: Stack) -> None:
    rid = (await stack.pr("opened")).json()["review_id"]
    for _ in range(MAX_RECOVERIES):
        await stack.age(rid)
        assert await stack.sweep() == ["requested"]  # 워커가 계속 메시지를 잃는다
    stack.bus.sent = []
    await stack.age(rid)

    assert await stack.sweep() == ["failed"]
    row = await stack.row(rid)
    assert (row["status"], row["recover_count"]) == ("failed", MAX_RECOVERIES + 1)
    assert stack.bus.sent == []
    assert stack.github.statuses[-1] == (HEAD, "failure")


async def test_two_pods_sweeping_at_once_publish_each_review_once(stack: Stack) -> None:
    """API 파드 2개가 동시에 sweep 해도 한 검토는 한 번만 다시 발행한다."""
    rids = []
    for i in range(6):
        sha = f"{i}" * 40
        stack.github.files[sha] = stack.github.files[HEAD]
        await stack.repo.insert_review(review_id=f"rv_{i}", app="sample-app", target_env="aws", repo_id=REPO,
                                       spec_ref={"repository": REPO, "commit": sha, "path": "deploy.yaml"},
                                       pr_head_sha=sha, requested_by="it", pr_number=None)
        await stack.age(f"rv_{i}")
        rids.append(f"rv_{i}")
    other_pod = ApiDeps(repo=PostgresReviewRepository(stack.pool), specs=stack.github, publisher=stack.bus,
                        github=stack.github)  # type: ignore[arg-type]

    a, b = await asyncio.gather(recover_stale_reviews(stack.api_deps), recover_stale_reviews(other_pod))

    assert sorted(r["review_id"] for r in a + b) == rids
    assert sorted(json.loads(v)["review_id"] for _, _, v in stack.bus.sent) == rids
    for rid in rids:
        assert (await stack.row(rid))["recover_count"] == 1


# --- 같은 SHA 중복 검토 (P2, 마이그레이션 0008) -------------------------------------------------------

async def test_same_webhook_twice_at_once_makes_one_review(stack: Stack) -> None:
    both_fetching = asyncio.Barrier(2)
    get_file = stack.github.get_file

    async def slow_get_file(*args: Any, **kwargs: Any) -> str:
        await both_fetching.wait()  # 둘 다 find_by_head 를 지난 뒤에 넣는다
        return await get_file(*args, **kwargs)

    stack.github.get_file = slow_get_file  # type: ignore[method-assign]
    a, b = await asyncio.gather(stack.pr("opened"), stack.pr("opened"))

    assert a.status_code == b.status_code == 202
    assert a.json()["review_id"] == b.json()["review_id"]
    assert len(await stack.repo.list_reviews([], 10)) == 1
    assert len(stack.bus.sent) == 1


async def test_head_unique_index_skips_failed_and_superseded(stack: Stack) -> None:
    def insert(rid: str) -> Any:
        return stack.repo.insert_review(review_id=rid, app="sample-app", target_env="aws", repo_id=REPO,
                                        spec_ref={"repository": REPO, "commit": HEAD, "path": "deploy.yaml"},
                                        pr_head_sha=HEAD, requested_by="it", pr_number=3)

    assert await insert("rv_1") == "rv_1"
    assert await insert("rv_2") == "rv_1"  # 겹치면 넣지 않고 기존 검토
    await stack.repo.update_review("rv_1", status="failed")
    assert await insert("rv_3") == "rv_3"
    await stack.repo.supersede_open(repository=REPO, pr_number=3, superseded_by=None, error="PR closed")
    assert await insert("rv_4") == "rv_4"
    assert await stack.repo.get_review("rv_2") is None


# --- /readyz (P2) — 실제 Postgres·Kafka --------------------------------------------------------------

async def test_readiness_checks_real_db_and_kafka(pool: Any) -> None:
    from aiokafka import AIOKafkaProducer

    from review_api.app import check_ready, make_readiness

    producer = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP)
    await producer.start()
    try:
        assert await check_ready(make_readiness(pool, producer)) == {"db": "ok", "kafka": "ok"}
        broken = make_pool(make_conninfo(DSN, port="1", connect_timeout="1"))  # 아무도 듣지 않는 포트
        await broken.open(wait=False)
        try:
            checks = await check_ready(make_readiness(broken, producer))
        finally:
            await broken.close()
        assert checks["db"] != "ok" and checks["kafka"] == "ok"
    finally:
        await producer.stop()
