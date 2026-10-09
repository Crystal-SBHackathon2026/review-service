"""실제 Postgres·Kafka 연동 — docker compose up -d kafka postgres 뒤에 돌린다. 환경변수가 없으면 건너뛴다.

    REVIEW_IT_DSN=postgresql://review:review@localhost:5432/oneaction_review KAFKA_BOOTSTRAP=localhost:9092 \
        .venv/bin/python -m pytest -q tests/integration

테스트마다 새 데이터베이스를 만들어 마이그레이션부터 적용한다. GitHub 만 가짜다.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import yaml
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.retrieval.file_retriever import FileRetriever
from review_api.app import ApiDeps, create_app
from review_common.github import SpecNotFound
from review_common.migrate import migrate
from review_common.repository import LIST_FIELDS, PostgresReviewRepository, make_pool
from review_worker.graph import Deps, build_graph
from review_worker.handler import ReviewHandler

DSN = os.environ.get("REVIEW_IT_DSN")
BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP")
pytestmark = pytest.mark.skipif(not (DSN and BOOTSTRAP), reason="REVIEW_IT_DSN·KAFKA_BOOTSTRAP 필요")

SAMPLES = Path(__file__).resolve().parents[2] / "ai" / "samples"
REPO = "Crystal-SBHackathon2026/sample-app"
HEAD = "a" * 40
MERGE_SHA = "c0ffee1" + "0" * 33


@pytest.fixture
async def conninfo() -> AsyncIterator[str]:
    name = f"review_it_{uuid.uuid4().hex[:8]}"
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(DSN, dbname=name)
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as admin:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
async def pool(conninfo: str) -> AsyncIterator[Any]:
    await migrate(conninfo)
    p = make_pool(conninfo)
    await p.open(wait=True)
    yield p
    await p.close()


class FakeGitHub:
    def __init__(self, spec_text: str) -> None:
        self.spec_text = spec_text
        self.merged: list[int] = []
        self.statuses: list[tuple[str, str]] = []
        self.suites: list[dict[str, Any]] = []

    async def check_suites(self, repository: str, sha: str) -> list[dict[str, Any]]:
        return self.suites

    async def prepare_file_commit(self, repository: str, **_: Any) -> str:
        raise AssertionError("수정 없는 샘플은 커밋하지 않는다")

    async def update_branch(self, repository: str, branch: str, sha: str) -> None:
        raise AssertionError("수정 없는 샘플은 커밋하지 않는다")

    async def get_file(self, repository: str, path: str, ref: str) -> str:
        if (repository, path, ref) != (REPO, "deploy.yaml", HEAD):
            raise SpecNotFound("없음", 404)
        return self.spec_text

    async def pulls_for_commit(self, repository: str, sha: str) -> list[dict[str, Any]]:
        return [{"number": 3, "state": "open", "head": {"sha": HEAD, "repo": {"full_name": REPO}}}]

    async def merge_pull(self, repository: str, number: int, *, head_sha: str) -> str:
        self.merged.append(number)
        return MERGE_SHA

    async def create_commit_status(self, repository: str, sha: str, *, state: str, context: str, description: str,
                                   target_url: str | None = None) -> None:
        self.statuses.append((context, state))


class NullPublisher:
    async def send(self, topic: str, key: str, value: bytes) -> None:
        raise AssertionError("수정 없는 샘플은 재검토를 발행하지 않는다")


def worker_graph(repo: PostgresReviewRepository, github: FakeGitHub, checkpointer: Any) -> Any:
    deps = Deps(repo=repo, github=github, publisher=NullPublisher(), llm=ScriptedLLM(oracle_review),
                retriever=FileRetriever())
    return build_graph(deps, checkpointer)


async def test_migrate_is_idempotent(conninfo: str) -> None:
    assert await migrate(conninfo) == ["0001_init.sql", "0002_superseded.sql", "0003_pr_number.sql",
                                       "0004_spec_intakes.sql", "0005_review_cases.sql",
                                       "0006_generated_spec_unverified.sql", "0007_review_recovery.sql",
                                       "0008_review_head_unique.sql", "0009_review_stage_times.sql",
                                       "0010_intake_attempts.sql"]
    assert await migrate(conninfo) == []


async def test_repository_on_postgres(pool: Any) -> None:
    repo = PostgresReviewRepository(pool)
    ref = {"repository": REPO, "commit": HEAD, "path": "deploy.yaml"}
    await repo.insert_review(review_id="rv_1", app="sample-app", target_env="aws", repo_id=REPO, spec_ref=ref,
                             pr_head_sha=HEAD, requested_by="it")

    assert await repo.claim("rv_1", from_statuses=["received"], to_status="reviewing")
    assert not await repo.claim("rv_1", from_statuses=["received"], to_status="reviewing")
    await repo.update_review("rv_1", status="waiting_ci", verdict="pass", reasons=["LOW_SCORE"],
                             findings=[{"rule_id": "NET-001"}], merge_sha=MERGE_SHA, final_spec={"a": 1})
    row = await repo.get_review("rv_1")
    assert (row["status"], row["reasons"], row["spec_ref"], row["findings"]) == (
        "waiting_ci", ["LOW_SCORE"], ref, [{"rule_id": "NET-001"}])
    assert [r["review_id"] for r in await repo.waiting_ci_by_head_sha(HEAD)] == ["rv_1"]
    assert not await repo.claim("rv_1", from_statuses=["waiting_ci"], to_status="merging", pr_head_sha="b" * 40)
    assert (await repo.find_by_merge_sha(app="sample-app", target_env="aws", image_tag="C0FFEE1"))["review_id"] == "rv_1"
    assert await repo.find_by_merge_sha(app="sample-app", target_env="aws", image_tag="c0ffe") is None

    now = datetime.now(UTC)
    await repo.upsert_baseline(app="sample-app", target_env="aws", spec={"v": 1}, spec_ref=ref, merge_sha="x",
                               observed_at=now)
    await repo.upsert_baseline(app="sample-app", target_env="aws", spec={"v": 2}, spec_ref=ref, merge_sha="y",
                               observed_at=now)
    baseline = await repo.get_baseline("sample-app", "aws")
    assert (baseline["spec"], baseline["merge_sha"], baseline["database_has_data"]) == ({"v": 2}, "y", None)
    await repo.add_deploy_event(review_id="rv_1", app="sample-app", target_env="aws", kind="healthy",
                                image_tag="c0ffee1", payload={"health": "Healthy"})
    assert await repo.has_deploy_event(review_id="rv_1", target_env="aws", kind="healthy", image_tag="c0ffee1")
    assert not await repo.has_deploy_event(review_id="rv_1", target_env="local", kind="healthy", image_tag="c0ffee1")
    assert not await repo.has_deploy_event(review_id="rv_1", target_env="aws", kind="degraded", image_tag="c0ffee1")
    assert not await repo.has_deploy_event(review_id="rv_1", target_env="aws", kind="healthy", image_tag=None)
    assert [(e["target_env"], e["kind"]) for e in await repo.deploy_events_for("rv_1")] == [("aws", "healthy")]
    assert (await repo.find_by_merge_sha(app="sample-app", target_env=None, image_tag="c0ffee1"))["review_id"] == "rv_1"
    assert await repo.find_by_merge_sha(app="sample-app", target_env="local", image_tag="c0ffee1") is None
    assert (await repo.find_by_merge_sha_exact(MERGE_SHA))["review_id"] == "rv_1"
    assert await repo.find_by_merge_sha_exact(MERGE_SHA[:7]) is None  # 정확히 같을 때만

    with pytest.raises(psycopg.errors.CheckViolation):
        await repo.update_review("rv_1", status="nope")
    with pytest.raises(psycopg.errors.CheckViolation):
        await repo.update_review("rv_1", reasons=["MADE_UP"])
    await repo.insert_review(review_id="rv_2", app="sample-app", target_env="aws", repo_id=REPO, spec_ref=ref,
                             pr_head_sha="b" * 40, requested_by="autofix:rv_1")
    await repo.update_review("rv_1", status="superseded", superseded_by="rv_2")
    assert (await repo.get_review("rv_1"))["superseded_by"] == "rv_2"
    assert (await repo.find_superseding_parent("rv_2"))["review_id"] == "rv_1"
    assert await repo.find_superseding_parent("rv_1") is None
    await repo.update_review("rv_1", status="committed", verdict="pass")  # superseded 상태는 덮어쓰지 않는다
    assert ((await repo.get_review("rv_1"))["status"], (await repo.get_review("rv_1"))["verdict"]) == (
        "superseded", "pass")

    # pull_request 웹훅: 같은 레포·head 검토 찾기, 같은 PR 의 끝나지 않은 검토 넘기기
    for rid, sha, pr, status in [("rv_a", "1" * 40, 9, "needs_human"), ("rv_b", "2" * 40, 9, "committed"),
                                 ("rv_c", "3" * 40, 10, "waiting_ci")]:
        await repo.insert_review(review_id=rid, app="sample-app", target_env="aws", repo_id=REPO,
                                 spec_ref={**ref, "commit": sha}, pr_head_sha=sha, requested_by="it", pr_number=pr)
        await repo.update_review(rid, status=status)
    assert (await repo.find_by_head(REPO, "1" * 40))["review_id"] == "rv_a"
    assert await repo.find_by_head("other/repo", "1" * 40) is None
    assert await repo.supersede_open(repository=REPO, pr_number=9, superseded_by="rv_2") == ["rv_a"]
    assert (await repo.get_review("rv_a"))["superseded_by"] == "rv_2"
    assert (await repo.get_review("rv_b"))["status"] == "committed"
    assert (await repo.get_review("rv_c"))["status"] == "waiting_ci"
    assert await repo.supersede_open(repository=REPO, pr_number=10, superseded_by=None,
                                     error="PR closed") == ["rv_c"]  # 병합 없이 닫힌 PR
    assert ((await repo.get_review("rv_c"))["superseded_by"], (await repo.get_review("rv_c"))["error"]) == (
        None, "PR closed")

    # 승인 화면 목록: 상태 필터, 최신순, limit, 목록 필드만
    listed = await repo.list_reviews(["committed", "received"], 10)
    assert [r["review_id"] for r in listed] == ["rv_b", "rv_2"]
    assert set(listed[0]) == set(LIST_FIELDS) and listed[0]["pr_number"] == 9
    assert [r["review_id"] for r in await repo.list_reviews(["committed", "received"], 1)] == ["rv_b"]
    assert [r["review_id"] for r in await repo.list_reviews([], 2)] == ["rv_c", "rv_b"]

    # failed 는 같은 SHA 가 다시 오면 새로 검토한다 — find_by_head 가 찾지 않는다
    await repo.update_review("rv_b", status="failed")
    assert await repo.find_by_head(REPO, "2" * 40) is None


async def test_spec_intakes_on_postgres(pool: Any) -> None:
    """명세 없음·빈 명세 기록 — 같은 head 한 번, processing 에서만 끝내기, 커밋·검토 잇기, 오래된 행 찾기."""
    repo = PostgresReviewRepository(pool)
    intake = {"intake_id": "in_1", "repository": REPO, "head_repository": REPO, "pr_number": 7, "head_sha": HEAD,
              "head_ref": "feature", "path": "deploy.yaml", "kind": "missing", "errors": [], "requested_by": "it"}
    assert await repo.insert_intake(**intake)
    assert not await repo.insert_intake(**{**intake, "intake_id": "in_dup"})
    assert (await repo.find_intake_by_head(REPO, HEAD))["intake_id"] == "in_1"
    assert (await repo.latest_intake_by_head_sha(HEAD))["status"] == "processing"
    assert (await repo.get_intake("in_1"))["attempts"] == 1
    assert not await repo.claim_stale_intakes(timedelta(minutes=5))
    assert [(r["intake_id"], r["attempts"]) for r in await repo.claim_stale_intakes(timedelta(0))] == [("in_1", 2)]
    assert not await repo.claim_stale_intakes(timedelta(seconds=30))  # 방금 가져간 행은 다른 곳이 못 가져간다

    assert await repo.unverified_generation_for_pr(REPO, 7) is None  # 아직 커밋 전
    assert await repo.link_intake("in_1", result_commit_sha="b" * 40) == "b" * 40
    assert await repo.link_intake("in_1", result_commit_sha="d" * 40) == "b" * 40  # 늦게 만든 커밋은 덮지 않는다
    assert (await repo.find_intake_by_result_commit(REPO, "b" * 40))["intake_id"] == "in_1"
    assert (await repo.unverified_generation_for_pr(REPO, 7))["intake_id"] == "in_1"  # baseline_used NULL → 모름
    await repo.link_intake("in_1", baseline_used=False)
    assert (await repo.unverified_generation_for_pr(REPO, 7))["baseline_used"] is False
    assert await repo.unverified_generation_for_pr(REPO, 8) is None
    async with pool.connection() as conn:
        await conn.execute("UPDATE spec_intakes SET updated_at = now() - interval '10 minutes' WHERE intake_id = 'in_1'")
    await repo.touch_intake("in_1")  # 처리 중 heartbeat
    assert not await repo.claim_stale_intakes(timedelta(minutes=5))
    assert await repo.finish_intake("in_1", status="generated", reason="GENERATED", message="m",
                                    details=[{"path": "/runtime"}], result_commit_sha="b" * 40)
    assert not await repo.finish_intake("in_1", status="failed", reason="ERROR", message="late")
    await repo.insert_review(review_id="rv_g", app="sample-app", target_env="aws", repo_id=REPO,
                             spec_ref={"repository": REPO, "commit": "b" * 40, "path": "deploy.yaml"},
                             pr_head_sha="b" * 40, requested_by="it", pr_number=7)
    await repo.link_intake("in_1", review_id="rv_g")
    row = await repo.get_intake("in_1")
    assert (row["status"], row["details"], row["review_id"], row["result_commit_sha"]) == (
        "generated", [{"path": "/runtime"}], "rv_g", "b" * 40)
    assert (await repo.find_intake_by_reviews(["rv_x", "rv_g"]))["intake_id"] == "in_1"
    assert await repo.find_intake_by_reviews(["rv_x"]) is None
    await repo.update_review("rv_g", verdict="needs_human", reasons=["GENERATED_SPEC_UNVERIFIED"])  # 0006 CHECK
    await repo.link_intake("in_1", baseline_used=True)  # 다시 처리해도 처음 만든 커밋을 쓴다 — 처음 값이 남는다
    assert (await repo.unverified_generation_for_pr(REPO, 7))["baseline_used"] is False
    await repo.insert_intake(**{**intake, "intake_id": "in_2", "head_sha": "c" * 40, "pr_number": 8})
    await repo.link_intake("in_2", result_commit_sha="d" * 40, baseline_used=True)
    assert await repo.unverified_generation_for_pr(REPO, 8) is None
    with pytest.raises(psycopg.errors.CheckViolation):
        await repo.insert_intake(**{**intake, "intake_id": "in_bad", "head_sha": "c" * 40, "kind": "nope"})

    # 배포 확인 전(baselines 비어 있음)엔 gitops 에 커밋한 검토의 final_spec 이 baseline 을 대신한다
    assert await repo.latest_baseline_for_repository(REPO) is None
    await repo.update_review("rv_g", status="committed", final_spec={"v": 0}, merge_sha="m" * 40)
    fallback = await repo.latest_baseline_for_repository(REPO)
    assert (fallback["spec"], fallback["merge_sha"], fallback["observed_at"], fallback["database_has_data"]) == (
        {"v": 0}, "m" * 40, None, None)
    await repo.upsert_baseline(app="sample-app", target_env="aws", spec={"v": 1},
                               spec_ref={"repository": REPO, "commit": HEAD, "path": "deploy.yaml"},
                               merge_sha="x", observed_at=datetime.now(UTC))
    assert (await repo.latest_baseline_for_repository(REPO))["spec"] == {"v": 1}
    assert await repo.latest_baseline_for_repository("other/repo") is None

async def test_review_cases_on_postgres(pool: Any) -> None:
    """판단 사례 — case_id 로 한 번만, rule_id 배열 매칭, 같은 앱·레포·허용한 종료 방식만, 같은 대상 환경 먼저·그 안에서 최근 순."""
    repo = PostgresReviewRepository(pool)
    for rid, repository in (("rv_1", REPO), ("rv_2", REPO), ("rv_3", REPO), ("rv_4", REPO), ("rv_5", "other/repo")):
        sha = rid[-1] * 40  # 같은 레포·SHA 검토는 하나뿐이다 (0008)
        await repo.insert_review(review_id=rid, app="sample-app", target_env="aws", repo_id=repository,
                                 spec_ref={"repository": repository, "commit": sha, "path": "deploy.yaml"},
                                 pr_head_sha=sha, requested_by="it")

    def case(rid: str, env: str, rules: list[str], *, app: str = "sample-app", outcome: str = "rejected"
             ) -> dict[str, Any]:
        return {"case_id": f"{rid}.{outcome}", "review_id": rid, "app": app, "target_env": env,
                "rule_ids": rules, "outcome": outcome, "summary": f"지난 검토 {rid}",
                "ops": [{"op": "replace", "path": "/database/engine", "value": "postgres"}]}

    def find(rule_id: str, env: str, limit: int, outcomes: tuple[str, ...] = ("rejected",)) -> Any:
        return repo.find_cases(rule_id, app="sample-app", repository=REPO, target_env=env, outcomes=outcomes,
                               limit=limit)

    assert await repo.insert_case(**case("rv_1", "aws", ["DB-001", "STO-005"]))
    assert await repo.insert_case(**case("rv_2", "gcp", ["DB-001"]))
    assert await repo.insert_case(**case("rv_3", "aws", ["DB-001"]))
    assert not await repo.insert_case(**case("rv_3", "aws", ["DB-001"]))
    assert await repo.insert_case(**case("rv_4", "aws", ["DB-001"], app="other-app"))  # 다른 앱
    assert await repo.insert_case(**case("rv_5", "aws", ["DB-001"]))  # 같은 앱 이름, 다른 레포
    assert await repo.insert_case(**case("rv_1", "aws", ["DB-001"], outcome="human_approved"))

    found = await find("DB-001", "aws", 5)
    assert [c["case_id"] for c in found] == ["rv_3.rejected", "rv_1.rejected", "rv_2.rejected"]
    assert found[0]["ops"] == [{"op": "replace", "path": "/database/engine", "value": "postgres"}]
    assert [c["case_id"] for c in await find("STO-005", "gcp", 3)] == ["rv_1.rejected"]
    assert len(await find("DB-001", "gcp", 2)) == 2
    assert [c["case_id"] for c in await find("DB-001", "aws", 1, ("rejected", "human_approved"))] == [
        "rv_1.human_approved"]
    with pytest.raises(psycopg.errors.CheckViolation):
        await repo.insert_case(**{**case("rv_1", "aws", ["DB-001"]), "case_id": "rv_1.x", "outcome": "merged"})


async def test_checkpoint_survives_worker_restart(pool: Any) -> None:
    """waiting_ci 에서 멈춘 검토를 새 워커(새 그래프·같은 DB 체크포인트)가 CI 결과로 이어 간다."""
    repo = PostgresReviewRepository(pool)
    github = FakeGitHub("")
    saver = AsyncPostgresSaver(pool)
    await saver.setup()

    from review_ai.messages import build_review_requested

    spec = yaml.safe_load((SAMPLES / "01-pass-sample-app-aws.yaml").read_text(encoding="utf-8"))
    ref = {"repository": REPO, "commit": HEAD, "path": "deploy.yaml"}
    msg = build_review_requested(spec, review_id="rv_ckpt", spec_ref=ref, requested_by="it",
                                 requested_at=datetime.now(UTC))
    await repo.insert_review(review_id="rv_ckpt", app=msg.app, target_env=msg.target_env, repo_id=msg.repo_id,
                             spec_ref=ref, pr_head_sha=HEAD, requested_by="it")

    first = ReviewHandler(repo, worker_graph(repo, github, saver))
    await first.handle("review.requested", msg.model_dump_json().encode())
    assert (await repo.get_review("rv_ckpt"))["status"] == "waiting_ci"

    github.suites = [{"status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}]
    restarted = ReviewHandler(repo, worker_graph(repo, github, AsyncPostgresSaver(pool)))
    resumed = {"schema_version": "review.resumed/v1", "review_id": "rv_ckpt", "kind": "ci_completed",
               "ci": {"head_sha": HEAD, "conclusion": "success"}, "resumed_at": datetime.now(UTC).isoformat()}
    await restarted.handle("review.resumed", json.dumps(resumed).encode())

    row = await repo.get_review("rv_ckpt")
    assert (row["status"], row["merge_sha"], github.merged) == ("blocked", MERGE_SHA, [3])


async def _ensure_topics(*names: str) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        new = [NewTopic(n, num_partitions=3, replication_factor=1) for n in names if n not in existing]
        if new:
            await admin.create_topics(new)
    finally:
        await admin.close()


async def test_api_to_kafka_to_worker(pool: Any) -> None:
    """POST /reviews → Kafka review.requested → 워커 → waiting_ci → 웹훅 → review.resumed → 병합."""
    import hashlib
    import hmac

    await _ensure_topics("review.requested", "review.resumed")
    repo = PostgresReviewRepository(pool)
    github = FakeGitHub((SAMPLES / "01-pass-sample-app-aws.yaml").read_text(encoding="utf-8"))
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    handler = ReviewHandler(repo, worker_graph(repo, github, saver))

    producer = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP, acks="all", enable_idempotence=True)
    await producer.start()

    class Publisher:
        async def send(self, topic: str, key: str, value: bytes) -> None:
            await producer.send_and_wait(topic, value=value, key=key.encode())

    consumer = AIOKafkaConsumer("review.requested", "review.resumed", bootstrap_servers=BOOTSTRAP,
                                group_id=f"it-{uuid.uuid4().hex[:6]}", enable_auto_commit=False,
                                auto_offset_reset="latest")
    await consumer.start()
    while not consumer.assignment():
        await consumer.getmany(timeout_ms=200)
    await consumer.seek_to_end()

    async def drain_until(review_id: str, status: str) -> None:
        for _ in range(100):
            for records in (await consumer.getmany(timeout_ms=200)).values():
                for record in records:
                    await handler.handle(record.topic, record.value)
                    await consumer.commit()
            row = await repo.get_review(review_id)
            if row and row["status"] == status:
                return
        raise AssertionError(f"{review_id} 가 {status} 가 되지 않았다: {row and row['status']}")

    secret = "it-secret"
    app = create_app(ApiDeps(repo=repo, specs=github, publisher=Publisher(), github_webhook_secret=secret,
                             api_token="it-api-token", argocd_webhook_token="it-argo-token"))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api",
                                     headers={"Authorization": "Bearer it-api-token"}) as client:
            resp = await client.post("/reviews", json={
                "spec_ref": {"repository": REPO, "commit": HEAD, "path": "deploy.yaml"}, "requested_by": "it"})
            assert resp.status_code == 202, resp.text
            rid = resp.json()["review_id"]
            await drain_until(rid, "waiting_ci")
            assert (await client.get("/verify", params={"sha": HEAD})).json()["passed"] is True

            github.suites = [{"status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}]
            body = json.dumps({"action": "completed", "check_suite": {
                "head_sha": HEAD, "conclusion": "success", "app": {"slug": "github-actions"}}}).encode()
            sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            resp = await client.post("/webhooks/github", content=body,
                                     headers={"X-GitHub-Event": "check_suite", "X-Hub-Signature-256": sig})
            assert resp.json() == {"resumed": [rid]}
            await drain_until(rid, "blocked")

            resp = await client.post("/webhooks/argocd", headers={"Authorization": "Bearer it-argo-token"}, json={
                "app": "sample-app", "env": "aws", "health": "Healthy",
                "images": [f"ghcr.io/crystal-sbhackathon2026/sample-app:{MERGE_SHA}"]})
            assert resp.json() == {"review_id": rid, "recorded": "healthy", "baseline": "updated"}
            assert (await repo.get_baseline("sample-app", "aws"))["merge_sha"] == MERGE_SHA
            detail = (await client.get(f"/reviews/{rid}")).json()
            assert (detail["verdict"], detail["merge_sha"]) == ("pass", MERGE_SHA)
    finally:
        await consumer.stop()
        await producer.stop()
