"""Review API — 검토 요청을 받아 review.requested 를 발행하고, 사람 결정·CI·배포 결과를 받아 검토를 이어 간다.

    POST /reviews                       deploy.yaml 검토 요청 → 202 {review_id}
    GET  /reviews/{review_id}           상태·verdict·사유·rounds·deploy_result
    GET  /verify?sha=<PR head SHA>      그 SHA 의 검토가 통과했는지 (CI 용)
    POST /reviews/{review_id}/decision  needs_human 검토에 사람 결정 → review.resumed(human_decision)
    POST /webhooks/github               check_suite completed → review.resumed(ci_completed)
    POST /webhooks/argocd               배포 Healthy·Degraded 기록, Healthy 면 baselines 갱신
    GET  /healthz
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import yaml
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_ai.graph import check_edited_ops
from review_ai.messages import TOPIC as REQUESTED_TOPIC
from review_ai.messages import build_review_requested
from review_ai.spec.deploy_spec import REPOSITORY, DeploySpec
from review_api.argocd import ArgoCdEvent
from review_common.github import GitHubError, SpecNotFound
from review_common.ids import new_review_id
from review_common.repository import ReviewRepository
from review_common.resumed import TOPIC as RESUMED_TOPIC
from review_common.resumed import CiCompletedResumed, CiResult, HumanDecisionModel, HumanDecisionResumed

log = logging.getLogger(__name__)

PASSED_STATUSES = frozenset({"waiting_ci", "merging", "committed"})  # 검토를 통과(사람 승인 포함)한 뒤의 상태


class SpecSource(Protocol):
    async def get_file(self, repository: str, path: str, ref: str) -> str: ...


class Publisher(Protocol):
    async def send(self, topic: str, key: str, value: bytes) -> None: ...


@dataclass
class ApiDeps:
    repo: ReviewRepository
    specs: SpecSource
    publisher: Publisher
    github_webhook_secret: str | None = None
    argocd_webhook_token: str | None = None
    ci_app_slug: str | None = "github-actions"  # 이 GitHub App 의 check_suite 만 CI 결과로 본다. None 이면 전부


class SpecRefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(pattern=REPOSITORY, description="owner/repo")
    commit: str = Field(pattern=r"^[0-9a-f]{7,40}$", description="PR head SHA")
    path: str = Field(default="deploy.yaml", min_length=1)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec_ref: SpecRefIn
    requested_by: str = Field(min_length=1)


def _errors(exc: ValidationError) -> list[dict[str, Any]]:
    return json.loads(exc.json(include_url=False, include_input=False))


def create_app(deps: ApiDeps | None = None) -> FastAPI:
    lifespan = None if deps is not None else _real_lifespan
    app = FastAPI(title="review-service", lifespan=lifespan)
    if deps is not None:
        app.state.deps = deps

    def d(request: Request) -> ApiDeps:
        return request.app.state.deps

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/reviews", status_code=202)
    async def create_review(body: ReviewIn, request: Request) -> dict[str, str]:
        deps_ = d(request)
        ref = body.spec_ref
        try:
            raw = await deps_.specs.get_file(ref.repository, ref.path, ref.commit)
        except SpecNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except GitHubError as exc:
            raise HTTPException(502, str(exc)) from exc
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise HTTPException(422, {"message": f"{ref.path} YAML 파싱 실패", "errors": str(exc)}) from exc
        try:
            DeploySpec.model_validate(loaded)
        except ValidationError as exc:
            raise HTTPException(422, {"message": "deploy_spec 형식 오류", "errors": _errors(exc)}) from exc

        review_id = new_review_id()
        spec_ref = ref.model_dump()
        message = build_review_requested(loaded, review_id=review_id,
                                         spec_ref=spec_ref, requested_by=body.requested_by,
                                         requested_at=datetime.now(UTC))
        await deps_.repo.insert_review(review_id=review_id, app=message.app, target_env=message.target_env,
                                       repo_id=message.repo_id, spec_ref=spec_ref, pr_head_sha=ref.commit,
                                       requested_by=body.requested_by)
        try:
            await deps_.publisher.send(REQUESTED_TOPIC, message.repo_id, message.model_dump_json().encode())
        except Exception as exc:
            log.exception("review %s: review.requested 발행 실패", review_id)
            await deps_.repo.update_review(review_id, status="failed", error=f"publish: {exc}"[:2000])
            raise HTTPException(503, "검토 요청을 큐에 넣지 못했다") from exc
        return {"review_id": review_id}

    @app.get("/reviews/{review_id}")
    async def get_review(review_id: str, request: Request) -> dict[str, Any]:
        row = await d(request).repo.get_review(review_id)
        if row is None:
            raise HTTPException(404, "검토가 없다")
        keys = ("review_id", "app", "target_env", "spec_ref", "status", "verdict", "reasons", "findings", "decision",
                "rounds", "human_decision", "deploy_result", "merge_sha", "gitops_commit_sha", "error",
                "superseded_by", "requested_by", "created_at", "updated_at")
        return {k: row.get(k) for k in keys}

    @app.get("/verify")
    async def verify(request: Request, sha: str = Query(min_length=7)) -> dict[str, Any]:
        row = await d(request).repo.latest_by_head_sha(sha)
        if row is None:
            raise HTTPException(404, f"{sha} 에 대한 검토가 없다")
        return {"sha": sha, "review_id": row["review_id"], "passed": row["status"] in PASSED_STATUSES,
                "status": row["status"], "verdict": row["verdict"], "reasons": row["reasons"] or []}

    @app.post("/reviews/{review_id}/decision", status_code=202)
    async def decide(review_id: str, body: HumanDecisionModel, request: Request) -> dict[str, str]:
        deps_ = d(request)
        row = await deps_.repo.get_review(review_id)
        if row is None:
            raise HTTPException(404, "검토가 없다")
        if row["status"] != "needs_human":
            raise HTTPException(409, f"needs_human 상태에서만 결정할 수 있다 (지금 {row['status']})")
        if body.decision == "approved" and body.edited_ops:
            # edited_ops 는 멈춘 State 의 deploy_spec 기준 — 워커가 멈출 때 final_spec 에 남긴 것과 같다
            if row["final_spec"] is None:
                raise HTTPException(409, "검토 중인 명세가 아직 기록되지 않았다")
            try:
                check_edited_ops(row["final_spec"], [op.as_op() for op in body.edited_ops])
            except ValueError as exc:
                raise HTTPException(422, {"message": "edited_ops 를 적용할 수 없다", "errors": str(exc)}) from exc
        msg = HumanDecisionResumed(review_id=review_id, human_decision=body, resumed_at=datetime.now(UTC))
        await deps_.publisher.send(RESUMED_TOPIC, review_id, msg.model_dump_json().encode())
        return {"review_id": review_id}

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(
        request: Request,
        x_github_event: str = Header(default=""),
        x_hub_signature_256: str = Header(default=""),
    ) -> dict[str, Any]:
        deps_ = d(request)
        body = await request.body()
        if not deps_.github_webhook_secret:
            raise HTTPException(503, "GITHUB_WEBHOOK_SECRET 이 설정되지 않았다")
        expected = "sha256=" + hmac.new(deps_.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(401, "서명이 맞지 않다")
        if x_github_event != "check_suite":
            return {"ignored": f"event {x_github_event}"}
        payload = json.loads(body)
        if payload.get("action") != "completed":
            return {"ignored": f"action {payload.get('action')}"}
        suite = payload.get("check_suite") or {}
        slug = (suite.get("app") or {}).get("slug")
        if deps_.ci_app_slug and slug != deps_.ci_app_slug:
            return {"ignored": f"app {slug}"}
        ci = CiResult(head_sha=suite["head_sha"], conclusion=suite.get("conclusion") or "unknown")
        resumed = []
        for row in await deps_.repo.waiting_ci_by_head_sha(ci.head_sha):
            msg = CiCompletedResumed(review_id=row["review_id"], ci=ci, resumed_at=datetime.now(UTC))
            await deps_.publisher.send(RESUMED_TOPIC, row["review_id"], msg.model_dump_json().encode())
            resumed.append(row["review_id"])
        return {"resumed": resumed}

    @app.post("/webhooks/argocd", status_code=202)
    async def argocd_webhook(event: ArgoCdEvent, request: Request,
                             authorization: str = Header(default="")) -> dict[str, Any]:
        deps_ = d(request)
        if deps_.argocd_webhook_token and not hmac.compare_digest(authorization,
                                                                  f"Bearer {deps_.argocd_webhook_token}"):
            raise HTTPException(401, "토큰이 맞지 않다")
        kind = {"Healthy": "healthy", "Degraded": "degraded"}.get(event.health)
        if kind is None:
            return {"ignored": f"health {event.health}"}
        for tag in event.image_tags():
            row = await deps_.repo.find_by_merge_sha(app=event.app, target_env=event.env, image_tag=tag)
            if row is None:
                continue
            await deps_.repo.add_deploy_event(review_id=row["review_id"], app=event.app, target_env=event.env,
                                              kind=kind, image_tag=tag, payload=event.model_dump(mode="json"))
            if kind == "healthy" and row.get("final_spec"):
                await deps_.repo.upsert_baseline(app=event.app, target_env=event.env, spec=row["final_spec"],
                                                 spec_ref=row["spec_ref"], merge_sha=row["merge_sha"],
                                                 observed_at=datetime.now(UTC))
            return {"review_id": row["review_id"], "recorded": kind}
        return {"ignored": "이미지 태그와 맞는 병합 SHA 가 없다"}

    return app


@asynccontextmanager
async def _real_lifespan(app: FastAPI) -> AsyncIterator[None]:
    import os

    from aiokafka import AIOKafkaProducer

    from review_common.github import GitHubClient
    from review_common.migrate import migrate
    from review_common.repository import PostgresReviewRepository, make_pool
    from review_common.settings import db_conninfo, kafka_bootstrap

    conninfo = db_conninfo()
    await migrate(conninfo)
    pool = make_pool(conninfo)
    await pool.open(wait=True)
    producer = AIOKafkaProducer(bootstrap_servers=kafka_bootstrap(), acks="all", enable_idempotence=True)
    await producer.start()
    github = GitHubClient()

    class KafkaPublisher:
        async def send(self, topic: str, key: str, value: bytes) -> None:
            await producer.send_and_wait(topic, value=value, key=key.encode())

    app.state.deps = ApiDeps(
        repo=PostgresReviewRepository(pool),
        specs=github,
        publisher=KafkaPublisher(),
        github_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET"),
        argocd_webhook_token=os.environ.get("ARGOCD_WEBHOOK_TOKEN") or None,
        ci_app_slug=os.environ.get("GITHUB_CI_APP_SLUG", "github-actions") or None,
    )
    try:
        yield
    finally:
        await producer.stop()
        await github.aclose()
        await pool.close()


app = create_app()
