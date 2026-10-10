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
                                       "0010_intake_attempts.sql", "0011_deployment_analysis.sql",
                                       "0012_intake_unverified_paths.sql", "0013_deployment_requests.sql"]
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


@pytest.mark.parametrize("sample,large_requests", [
    ("23-fix-busan-resource-limits.yaml", False),
    ("24-human-tokyo-manual-bluegreen.yaml", False),
    ("23-fix-busan-resource-limits.yaml", True),
])
@pytest.mark.parametrize("deterministic,strict", [(False, False), (True, False), (False, True), (True, True)])
async def test_demo_api_kafka_postgres_fix_and_approval(
    pool: Any, sample: str, large_requests: bool, deterministic: bool, strict: bool,
) -> None:
    """실DB·실Kafka·실승인 API·체크포인트. Claude·GitHub·GitOps 는 외부 변경 없이 대체한다."""
    from review_ai.overlay import render_overlay

    fix_sha = "b" * 40
    original = yaml.safe_load((SAMPLES / sample).read_text())
    if large_requests:
        original["runtime"]["resources"]["memory_request"] = "256Mi"
    repo = PostgresReviewRepository(pool)

    class DemoGitHub(FakeGitHub):
        def __init__(self):
            super().__init__(yaml.safe_dump(original, sort_keys=False))
            self.head = HEAD
            self.files = {HEAD: self.spec_text}
            self.prepared = None
            self.commits = []
            self.head_statuses = []

        async def get_file(self, repository, path, ref):
            if path != "deploy.yaml":
                raise SpecNotFound("missing", 404)
            assert repository == REPO
            return self.files[ref]

        async def pulls_for_commit(self, repository, sha):
            return [{"number": 3, "state": "open", "head": {
                "sha": self.head, "ref": "demo-local", "repo": {"full_name": REPO}}}]

        async def prepare_file_commit(self, repository, **kwargs):
            assert kwargs["parent"] == self.head and self.prepared is None
            self.prepared = kwargs
            return fix_sha

        async def update_branch(self, repository, branch, sha):
            assert sha == fix_sha and self.prepared is not None
            self.files[sha] = self.prepared["content"]
            self.commits.append(self.prepared)
            self.prepared = None
            self.head = sha

        async def merge_pull(self, repository, number, *, head_sha):
            assert head_sha == self.head
            return await super().merge_pull(repository, number, head_sha=head_sha)

        async def create_commit_status(self, repository, sha, **kwargs):
            self.head_statuses.append((sha, kwargs["state"]))
            await super().create_commit_status(repository, sha, **kwargs)

    github = DemoGitHub()
    await _ensure_topics("review.requested", "review.resumed")
    producer = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP, acks="all", enable_idempotence=True)
    await producer.start()

    class Publisher:
        async def send(self, topic, key, value):
            await producer.send_and_wait(topic, value=value, key=key.encode())

    publisher = Publisher()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    rendered = []

    async def local_overlay(state):
        rendered.append(render_overlay(DeploySpec.model_validate(state["deploy_spec"])))
        # 로컬 렌더링까지만 검증한다. GitOps commit 은 수행하지 않는다.
        return {"deploy_result": {"status": "blocked", "commit_sha": None, "reason": "LOCAL_RENDER_ONLY"}}

    from review_ai.spec.deploy_spec import DeploySpec
    deps = Deps(repo=repo, github=github, publisher=publisher, llm=ScriptedLLM(oracle_review),
                retriever=FileRetriever(), strict_citations=strict, deterministic_fixes=deterministic,
                commit_overlay=local_overlay)
    handler = ReviewHandler(repo, build_graph(deps, saver))
    consumer = AIOKafkaConsumer("review.requested", "review.resumed", bootstrap_servers=BOOTSTRAP,
                                group_id=f"demo-{uuid.uuid4().hex[:8]}", enable_auto_commit=False,
                                auto_offset_reset="latest")
    try:
        await consumer.start()
        async with asyncio.timeout(10):
            while not consumer.assignment():
                await consumer.getmany(timeout_ms=100)
            await consumer.seek_to_end()

        async def drain_until(rid, status):
            async with asyncio.timeout(20):
                while True:
                    for records in (await consumer.getmany(timeout_ms=100)).values():
                        for record in records:
                            await handler.handle(record.topic, record.value)
                            await consumer.commit()
                    row = await repo.get_review(rid)
                    if row and row["status"] == status:
                        return row

        app = create_app(ApiDeps(repo=repo, specs=github, publisher=publisher, api_token="local-it-token"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local",
                                     headers={"Authorization": "Bearer local-it-token"}) as client:
            response = await client.post("/reviews", json={"spec_ref": {
                "repository": REPO, "commit": HEAD, "path": "deploy.yaml"}, "requested_by": "local-demo"})
            assert response.status_code == 202, response.text
            rid = response.json()["review_id"]
            if original["target"]["env"] == "local":
                if large_requests:
                    paused = await drain_until(rid, "needs_human")
                    assert "PATCH_OUT_OF_SCOPE" in paused["reasons"] and not paused["decision"]["recommendations"]
                    human = {"decision": "approved", "approver": "demo"}
                    assert (await client.post(f"/reviews/{rid}/decision", json=human)).status_code == 422
                    bad = {**human, "edited_ops": [
                        {"op": "add", "path": "/runtime/resources/memory_limit", "value": "128Mi"},
                    ]}
                    assert (await client.post(f"/reviews/{rid}/decision", json=bad)).status_code == 422
                    assert (await repo.get_review(rid))["status"] == "needs_human"
                    assert not github.prepared and not github.commits and not github.merged
                    # PostgreSQL 체크포인트를 새 워커에서 재개한다.
                    handler = ReviewHandler(repo, build_graph(deps, AsyncPostgresSaver(pool)))
                    safe = {**human, "edited_ops": [
                        {"op": "add", "path": "/runtime/resources/cpu_limit", "value": "250m"},
                        {"op": "add", "path": "/runtime/resources/memory_limit", "value": "256Mi"},
                    ]}
                    response = await client.post(f"/reviews/{rid}/decision", json=safe)
                    assert response.status_code == 202, response.text
                parent = await drain_until(rid, "superseded")
                assert parent["rounds"][0]["verdict"] == ("needs_human" if large_requests else "fix")
                assert any(f["rule_id"] == "RUN-005" for f in parent["rounds"][0]["findings"])
                if large_requests:
                    assert parent["rounds"][0]["human"]["decision"] == "approved"
                else:
                    assert parent["rounds"][0]["patch_source"] == "llm"
                rid = parent["superseded_by"]
                row = await drain_until(rid, "waiting_ci")
                assert row["pr_head_sha"] == fix_sha and row["verdict"] == "pass"
                assert len(github.commits) == 1
                fixed = yaml.safe_load(github.files[fix_sha])
                assert fixed["runtime"]["resources"] == {**original["runtime"]["resources"],
                                                          "cpu_limit": "250m", "memory_limit": "256Mi" if large_requests else "128Mi"}
            else:
                row = await drain_until(rid, "needs_human")
                assert "AUTOFIX_FORBIDDEN" in row["reasons"] and not github.merged
                assert (await client.get("/verify", params={"sha": HEAD})).json()["passed"] is False
                # API 승인 → Kafka 메시지 → 재시작한 워커의 PostgreSQL 체크포인트 재개.
                handler = ReviewHandler(repo, build_graph(deps, AsyncPostgresSaver(pool)))
                response = await client.post(f"/reviews/{rid}/decision", json={"decision": "approved", "approver": "demo"})
                assert response.status_code == 202, response.text
                row = await drain_until(rid, "waiting_ci")
                assert row["human_decision"]["decision"] == "approved" and not github.commits
                assert row["final_spec"]["rollout"] == {"strategy": "bluegreen", "auto_promotion": False}
            assert (await client.get("/verify", params={"sha": github.head})).json()["passed"] is True
            assert (github.head, "success") in github.head_statuses and not github.merged
            github.suites = [{"status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}]
            await publisher.send("review.resumed", rid, json.dumps({"schema_version": "review.resumed/v1",
                "review_id": rid, "kind": "ci_completed", "ci": {"head_sha": github.head, "conclusion": "success"},
                "resumed_at": datetime.now(UTC).isoformat()}).encode())
            row = await drain_until(rid, "blocked")
            assert github.merged == [3] and row["deploy_result"]["reason"] == "LOCAL_RENDER_ONLY"
            assert len(rendered) == 1
            if original["target"]["env"] == "gcp":
                config = yaml.safe_load(rendered[0].files["kustomization.yaml"])
                strategy = next(op["value"] for patch in config["patches"] for op in yaml.safe_load(patch["patch"])
                                if op["path"] == "/spec/strategy")
                assert strategy["blueGreen"]["autoPromotionEnabled"] is False
    finally:
        await consumer.stop()
        await producer.stop()


async def seed_deployment_review(repo, rid, revision, *, readiness="/wrong"):
    import copy
    spec = yaml.safe_load((SAMPLES / "01-pass-sample-app-aws.yaml").read_text())
    spec["runtime"]["health"]["readiness"] = readiness
    await repo.insert_review(review_id=rid, app=spec["metadata"]["name"], target_env="aws", repo_id=REPO,
        spec_ref={"repository": REPO, "commit": revision, "path": "deploy.yaml"}, pr_head_sha=revision, requested_by="it")
    await repo.update_review(rid, status="committed", merge_sha=revision, gitops_commit_sha=revision, final_spec=spec)
    return spec


def deployment_payload(kind="sync_failed", revision="b"*40):
    return dict(schema_version="deployment.result/v1", event_type=kind, app="sample-app", env="aws",
        health="Healthy", sync_status="Synced", images=[], revision="a"*40, namespace="sample-app",
        cluster_id="it-cluster", operation=dict(phase="Succeeded" if kind == "deployed" else "Failed",
            revision=revision, message="probe failed to become ready", started_at="2026-10-09T14:00:00+09:00",
            finished_at="2026-10-09T14:01:00+09:00"))


async def test_deployment_outbox_atomic_deduplication_and_lease_recovery(pool):
    from review_api.argocd import ArgoCdEvent, handle_deploy_event
    from review_common.deployment import AnalysisRequested, dispatch_pending
    from review_worker.deployment_analysis import DeploymentAnalysisHandler
    from review_ai.judge.llm import LlmResponse
    repo = PostgresReviewRepository(pool)
    spec = await seed_deployment_review(repo, "rv_failure", "b"*40)
    results = await asyncio.gather(*(handle_deploy_event(repo, ArgoCdEvent.model_validate(deployment_payload())) for _ in range(20)))
    eid = results[0]["event_id"]
    assert sum(not r["duplicate"] for r in results) == 1
    assert len(await repo.list_deployments()) == 1
    assert len(await repo.deploy_events_for("rv_failure")) == 1
    assert await repo.get_failure_case(eid)
    assert (await repo.latest_baseline_for_repository(REPO)) is None
    class DownPublisher:
        async def send(self, *args):
            raise RuntimeError("broker down")
    assert await dispatch_pending(repo, DownPublisher()) == 0
    assert (await repo.get_deployment(eid))["analysis_status"] == "queued"
    abandoned = await repo.claim_analysis(eid)
    await repo._execute("UPDATE deployment_analysis_jobs SET lease_until=now()-interval '1 second' WHERE event_id=%s", (eid,))
    class Llm:
        async def complete(self, request):
            return LlmResponse(json.dumps(dict(summary="probe 경로 원인 후보", hypotheses=[dict(reason="probe 경로 확인 필요",
                evidence_ids=["e2"], spec_paths=["/runtime/health/readiness"], actions=["probe 경로 확인"])],
                additional_information=[], cited_case_ids=[])), "fake")
    await DeploymentAnalysisHandler(PostgresReviewRepository(pool), Llm()).handle(AnalysisRequested(event_id=eid).model_dump_json().encode())
    assert (await repo.get_deployment(eid))["analysis_status"] == "completed"
    assert not await repo.finish_analysis(eid, abandoned["lease_token"], status="failed", result={})
    assert (await repo.get_failure_case(eid))["diagnosis"]["spec_paths"] == ["/runtime/health/readiness"]
    from review_ai.deployment_analysis import case_advice
    advice = await case_advice(PostgresReviewRepository(pool), spec, REPO)
    assert advice["items"][0]["case_id"] == eid
    assert advice["items"][0]["applicability"] == "applicable"
    # Same attempt's additional evidence is retained, but only three analysis jobs are admitted.
    for i in range(3):
        body = deployment_payload(); body["operation"]["message"] = f"new evidence {i}"
        await handle_deploy_event(repo, ArgoCdEvent.model_validate(body))
    rows = await repo.list_deployments()
    assert len(rows) == 4 and sum(r["analysis_status"] == "skipped" for r in rows) == 1


async def test_deployment_api_kafka_analysis_and_verified_next_request(pool):
    from review_common.deployment import TOPIC as ANALYSIS_TOPIC
    from review_worker.deployment_analysis import DeploymentAnalysisHandler
    from review_ai.judge.llm import LlmResponse
    from review_ai.deployment_analysis import case_advice
    await _ensure_topics(ANALYSIS_TOPIC)
    repo = PostgresReviewRepository(pool)
    failed_spec = await seed_deployment_review(repo, "rv_failed", "b"*40)
    await seed_deployment_review(repo, "rv_old", "a"*40, readiness="/ready")
    producer = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP)
    consumer = AIOKafkaConsumer(ANALYSIS_TOPIC, bootstrap_servers=BOOTSTRAP, group_id="failure-it-"+uuid.uuid4().hex[:8],
        enable_auto_commit=False, auto_offset_reset="latest")
    await producer.start(); await consumer.start()
    while not consumer.assignment():
        await consumer.getmany(timeout_ms=200)
    await consumer.seek_to_end()
    class Publisher:
        async def send(self, topic, key, value):
            await producer.send_and_wait(topic, value=value, key=key.encode())
    class Llm:
        async def complete(self, req):
            return LlmResponse(json.dumps(dict(summary="probe 오류", hypotheses=[dict(reason="경로 불일치 후보",
                evidence_ids=["e2"], spec_paths=["/runtime/health/readiness"], actions=["readiness 경로 확인"])],
                additional_information=[], cited_case_ids=[])), "fake")
    app = create_app(ApiDeps(repo=repo, specs=None, publisher=Publisher(), api_token="operator", argocd_webhook_token="argo"))
    handler = DeploymentAnalysisHandler(repo, Llm())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://it") as c:
            auth = {"Authorization": "Bearer argo"}
            assert (await c.post("/webhooks/argocd", json=deployment_payload("deployed", "a"*40), headers=auth)).status_code == 202
            r = await c.post("/webhooks/argocd", json=deployment_payload(), headers=auth)
            assert r.status_code == 202
            eid = r.json()["event_id"]
            for _ in range(50):
                for records in (await consumer.getmany(timeout_ms=200)).values():
                    for record in records:
                        assert set(json.loads(record.value)) == {"schema_version", "event_id"}
                        await handler.handle(record.value); await consumer.commit()
                if (await repo.get_deployment(eid))["analysis_status"] == "completed":
                    break
            assert (await repo.get_deployment(eid))["analysis_status"] == "completed"
            assert (await c.get(f"/deployments/{eid}")).status_code == 401
            assert (await repo.get_baseline("sample-app", "aws"))["merge_sha"] == "a"*40
            fixed_spec = await seed_deployment_review(repo, "rv_fixed", "c"*40, readiness="/ready")
            body = deployment_payload("deployed", "c"*40)
            body["operation"]["started_at"] = "2026-10-09T15:00:00+09:00"
            body["operation"]["finished_at"] = "2026-10-09T15:01:00+09:00"
            success = (await c.post("/webhooks/argocd", json=body, headers=auth)).json()["event_id"]
            resolution = await c.post(f"/failure-cases/{eid}/resolution", headers={"Authorization": "Bearer operator"},
                json=dict(success_event_id=success, cause="경로 불일치 확인", actions=["경로 수정 확인"], config_paths=["/runtime/health/readiness"]))
            assert resolution.status_code == 200, resolution.text
            # Re-created repository proves persistence; static findings are not needed for this path.
            advice = await case_advice(PostgresReviewRepository(pool), fixed_spec, REPO)
            resolved = next(a for a in advice["items"] if a["case_id"] == eid)
            assert resolved["applicability"] == "resolved" and resolved["actions"] == []
            assert advice["observation"]["match_count"] == 0
            recurrence = await case_advice(PostgresReviewRepository(pool), failed_spec, REPO)
            assert recurrence["observation"]["matched_case_ids"] == [eid]
            await repo.update_review("rv_failed", decision={"failure_case_observation": recurrence["observation"]})
            assert (await repo.get_review("rv_failed"))["decision"]["failure_case_observation"]["match_count"] == 1
            assert (await repo.get_review("rv_failed"))["status"] == "committed"
    finally:
        await consumer.stop(); await producer.stop()


async def test_cross_env_baseline_isolation_and_progress_on_postgres(pool):
    from review_api.argocd import ArgoCdEvent, handle_deploy_event
    from review_api.progress import build_progress, ProgressSettings
    repo = PostgresReviewRepository(pool)
    await seed_deployment_review(repo, "rv_scope", "b"*40)
    healthy = ArgoCdEvent.model_validate(deployment_payload("deployed"))
    await handle_deploy_event(repo, healthy)
    await repo._execute("UPDATE baselines SET database_has_data=true WHERE app='sample-app' AND target_env='aws'", ())
    local = deployment_payload(); local["env"] = "local"
    local["cluster_id"] = "crystal-busan"
    results = await asyncio.gather(*(handle_deploy_event(repo, ArgoCdEvent.model_validate(local)) for _ in range(10)))
    assert all(r["cross_env"] and not r["linked"] for r in results)
    assert len([e for e in await repo.deploy_events_for("rv_scope") if e["target_env"] == "local"]) == 1
    assert not await repo.has_failed_deployment("rv_scope")
    assert (await repo.get_baseline("sample-app", "aws"))["database_has_data"] is True
    assert (await repo.latest_baseline_for_repository(REPO))["merge_sha"] == "b"*40
    assert await repo.get_baseline("sample-app", "local") is None
    assert await repo.find_failure_cases(app="sample-app", repository=REPO, target_env="aws") == []
    # Existing historical cross-env healthy rows cannot qualify as a target baseline.
    await repo._execute("DELETE FROM baselines", ())
    await repo._execute("DELETE FROM deploy_events WHERE target_env='aws'", ())
    await repo._execute("DELETE FROM deployment_observations WHERE kind='deployed'", ())
    assert await repo.get_baseline("sample-app", "aws") is None
    # Target failure is visible even alongside healthy secondary environment notifications.
    await handle_deploy_event(repo, ArgoCdEvent.model_validate(deployment_payload()))
    local["event_type"] = "deployed"; local["operation"]["phase"] = "Succeeded"
    await handle_deploy_event(repo, ArgoCdEvent.model_validate(local))
    progress = await build_progress(repo, "rv_scope", ProgressSettings())
    assert next(s for s in progress["steps"] if s["key"] == "deploy")["state"] == "failed"
    assert progress["review"]["deployment"]["status"] == "failed"


async def test_last_deploy_state_decides_baseline_on_postgres(pool):
    """실패가 있었나가 아니라 마지막 알림이 실패인가 — LAST_DEPLOY_FAILED SQL 을 실제 Postgres 로 고정한다."""
    from review_api.argocd import ArgoCdEvent, handle_deploy_event
    from review_api.progress import build_progress, ProgressSettings
    repo = PostgresReviewRepository(pool)
    await seed_deployment_review(repo, "rv_flap", "b"*40)

    async def legacy(health):
        event = dict(app="sample-app", env="aws", health=health, images=["org/app:" + "b"*40], revision="c"*40)
        return await handle_deploy_event(repo, ArgoCdEvent.model_validate(event))

    async def state():
        progress = await build_progress(repo, "rv_flap", ProgressSettings())
        return (next(s for s in progress["steps"] if s["key"] == "deploy")["state"],
                progress["review"]["deployment"]["status"])

    await legacy("Degraded")
    assert await repo.is_deployment_failing("rv_flap") and await repo.get_baseline("sample-app", "aws") is None
    assert (await legacy("Healthy"))["baseline"] == "updated"
    assert not await repo.is_deployment_failing("rv_flap")
    assert (await repo.get_baseline("sample-app", "aws"))["merge_sha"] == "b"*40
    assert (await repo.latest_baseline_for_repository(REPO))["merge_sha"] == "b"*40
    assert (await state())[0] == "done" and (await state())[1] != "failed"
    await legacy("Degraded")  # 회복 뒤 다시 실패 — 같은 Degraded 가 전에 왔어도 전이라 기록한다
    assert await repo.is_deployment_failing("rv_flap")
    assert await repo.get_baseline("sample-app", "aws") is None
    assert await state() == ("failed", "failed")
    assert (await legacy("Degraded"))["duplicate"]
    assert not (await legacy("Healthy")).get("duplicate")
    assert (await repo.get_baseline("sample-app", "aws"))["merge_sha"] == "b"*40
    assert await repo.has_failed_deployment("rv_flap")  # 해결 근거 자격은 '한 번이라도'를 유지


async def test_chunk_evidence_and_deterministic_source_persist_without_migration(pool):
    from review_ai.graph import initial_state, run_graph
    from review_api.progress import build_progress, ProgressSettings
    spec = yaml.safe_load((SAMPLES / '07-fix-public-bucket.yaml').read_text())
    final = await run_graph(initial_state(spec, review_id='rv_evidence'), llm=None,
                            retriever=FileRetriever(), deterministic_fixes=True, strict_citations=True)
    repo = PostgresReviewRepository(pool)
    await repo.insert_review(review_id='rv_evidence', app=spec['metadata']['name'], target_env='aws', repo_id=REPO,
                            spec_ref={'repository': REPO, 'commit': HEAD, 'path': 'deploy.yaml'},
                            pr_head_sha=HEAD, requested_by='it')
    await repo.update_review('rv_evidence', status='waiting_ci', verdict='pass', decision=final['decision'],
                             rounds=final['rounds'], findings=final['findings'], final_spec=final['deploy_spec'])
    recreated = PostgresReviewRepository(pool)
    progress = await build_progress(recreated, 'rv_evidence', ProgressSettings())
    snapshot = progress['history'][0]['rounds'][0]
    assert snapshot['patch_source'] == 'deterministic'
    evidence = snapshot['items'][0]['evidence'][0]
    assert evidence['rule_id'] == 'STO-003' and evidence['excerpt'] and len(evidence['content_hash']) == 64
