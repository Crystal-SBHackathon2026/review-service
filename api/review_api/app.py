"""Review API — 검토 요청을 받아 review.requested 를 발행하고, 사람 결정·CI·배포 결과를 받아 검토를 이어 간다.

    POST /reviews                       deploy.yaml 검토 요청 → 202 {review_id}
    GET  /reviews/{review_id}           상태·verdict·사유·rounds·deploy_result
    GET  /verify?sha=<PR head SHA>      그 SHA 의 검토가 통과했는지 (CI 용). 명세가 없거나 깨진 SHA 는 intake 상태
    GET  /intakes/{intake_id}           명세 없음·빈 명세·형식 오류 처리 기록 (PR 커밋 상태의 링크)
    POST /reviews/{review_id}/decision  needs_human 검토에 사람 결정 → review.resumed(human_decision)
    POST /webhooks/github               pull_request opened·synchronize·reopened → 검토 시작
                                        (deploy.yaml 없음·빈 파일·형식 오류 → spec_intakes, review_api.intake)
                                        check_suite completed → review.resumed(ci_completed)
    POST /webhooks/argocd               배포 Healthy·Degraded 기록, Healthy 면 baselines 갱신, Degraded 면 판단 사례
    GET  /healthz
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from review_ai.graph import check_edited_ops
from review_ai.intake.repair import RepairOutput
from review_ai.judge.llm import CachedLLM, ClaudeLLM, LlmClient, LlmUnavailable
from review_ai.messages import TOPIC as REQUESTED_TOPIC
from review_ai.messages import build_review_requested
from review_ai.recommendations import resolve_human_decision
from review_ai.spec.deploy_spec import REPOSITORY
from review_ai.state import REASON_MESSAGES
from review_ai.transform import TRANSFORM_MAX_TOKENS
from review_ai.transform.prompt import TransformOutput
from review_api.argocd import ArgoCdEvent, handle_deploy_event
from review_api.intake import (IntakeGitHub, SpecProblem, expects_spec, load_spec, open_intake, process_intake,
                               sweep_stale_intakes)
from review_api.lockfile import regenerate_lockfile
from review_common.github import GitHubError, SpecNotFound
from review_common.ids import new_review_id
from review_common.repository import ReviewRepository
from review_common.resumed import TOPIC as RESUMED_TOPIC
from review_common.resumed import CiCompletedResumed, CiResult, HumanDecisionModel, HumanDecisionResumed

log = logging.getLogger(__name__)

PASSED_STATUSES = frozenset({"waiting_ci", "merging", "committed"})  # 검토를 통과(사람 승인 포함)한 뒤의 상태
INTAKE_FAILED = frozenset({"rejected", "failed"})


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
    github: IntakeGitHub | None = None  # 명세 생성 커밋·PR 커밋 상태. None 이면 intake 는 기록만 하고 거절
    default_target: str | None = None   # "aws/ap-northeast-2" — baseline 없는 레포의 명세를 만들 대상
    public_url: str | None = None       # PR 커밋 상태 링크(/intakes/{id})의 앞부분
    intake_repositories: frozenset[str] = frozenset()  # baseline 이 없어도 deploy.yaml 없음을 intake 로 볼 레포
    repair_llm: LlmClient | None = None  # 형식 오류 명세 복구. None 이면 yaml_error·schema_error 는 REPAIR_UNAVAILABLE
    transform_llm: LlmClient | None = None  # 새 앱 코드 패치(SQLite→Postgres·/metrics). None 이면 TRANSFORM_UNAVAILABLE
    lockfile: Callable[[str, str], Awaitable[str]] = regenerate_lockfile  # 코드 패치가 바꾼 의존성의 잠금 파일


class SpecRefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(pattern=REPOSITORY, description="owner/repo")
    commit: str = Field(pattern=r"^[0-9a-f]{7,40}$", description="PR head SHA")
    path: str = Field(default="deploy.yaml", min_length=1)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec_ref: SpecRefIn
    requested_by: str = Field(min_length=1)


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
        try:
            review_id = await start_review(d(request), body.spec_ref.model_dump(), body.requested_by)
        except SpecNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"review_id": review_id}

    @app.get("/reviews/{review_id}")
    async def get_review(review_id: str, request: Request) -> dict[str, Any]:
        row = await d(request).repo.get_review(review_id)
        if row is None:
            raise HTTPException(404, "검토가 없다")
        keys = ("review_id", "app", "target_env", "spec_ref", "status", "verdict", "reasons", "findings", "decision",
                "rounds", "human_decision", "deploy_result", "merge_sha", "gitops_commit_sha", "error",
                "superseded_by", "requested_by", "created_at", "updated_at")
        reasons = row.get("reasons") or []
        return {**{k: row.get(k) for k in keys},
                "reason_messages": {code: REASON_MESSAGES[code] for code in reasons if code in REASON_MESSAGES}}

    @app.get("/verify")
    async def verify(request: Request, sha: str = Query(min_length=7)) -> dict[str, Any]:
        row = await d(request).repo.latest_by_head_sha(sha)
        if row is None:
            intake = await d(request).repo.latest_intake_by_head_sha(sha)
            if intake is None:
                raise HTTPException(404, f"{sha} 에 대한 검토가 없다")
            status = "intake_failed" if intake["status"] in INTAKE_FAILED else f"intake_{intake['status']}"
            return {"sha": sha, "intake_id": intake["intake_id"], "passed": False, "status": status,
                    "verdict": None, "reasons": [intake["reason"]] if intake["reason"] else []}
        return {"sha": sha, "review_id": row["review_id"], "passed": row["status"] in PASSED_STATUSES,
                "status": row["status"], "verdict": row["verdict"], "reasons": row["reasons"] or []}

    @app.get("/intakes/{intake_id}")
    async def get_intake(intake_id: str, request: Request) -> dict[str, Any]:
        row = await d(request).repo.get_intake(intake_id)
        if row is None:
            raise HTTPException(404, "intake 가 없다")
        keys = ("intake_id", "repository", "pr_number", "head_sha", "path", "kind", "errors", "status", "reason",
                "message", "details", "result_commit_sha", "baseline_used", "review_id", "requested_by", "created_at",
                "updated_at")
        return {k: row.get(k) for k in keys}

    @app.post("/reviews/{review_id}/decision", status_code=202)
    async def decide(review_id: str, body: HumanDecisionModel, request: Request) -> dict[str, str]:
        deps_ = d(request)
        row = await deps_.repo.get_review(review_id)
        if row is None:
            raise HTTPException(404, "검토가 없다")
        if row["status"] != "needs_human":
            raise HTTPException(409, f"needs_human 상태에서만 결정할 수 있다 (지금 {row['status']})")
        if body.decision == "approved" and (body.edited_ops or body.use_recommendations):
            # edited_ops 는 멈춘 State 의 deploy_spec 기준 — 워커가 멈출 때 final_spec 에 남긴 것과 같다
            if row["final_spec"] is None:
                raise HTTPException(409, "검토 중인 명세가 아직 기록되지 않았다")
            try:
                resolved = resolve_human_decision({"deploy_spec": row["final_spec"], "decision": row.get("decision")},
                                                 body.as_state())
                check_edited_ops(row["final_spec"], resolved["edited_ops"])
            except ValueError as exc:
                raise HTTPException(422, {"message": "edited_ops 를 적용할 수 없다", "errors": str(exc)}) from exc
        msg = HumanDecisionResumed(review_id=review_id, human_decision=body, resumed_at=datetime.now(UTC))
        await deps_.publisher.send(RESUMED_TOPIC, review_id, msg.model_dump_json().encode())
        return {"review_id": review_id}

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(
        request: Request,
        background: BackgroundTasks,
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
        payload = json.loads(body)
        if x_github_event == "pull_request":
            result = await on_pull_request(deps_, payload)
            if "kind" in result:  # 새 intake — 생성·커밋은 응답 뒤에 (웹훅 10초 제한)
                background.add_task(process_intake, deps_, result["intake_id"])
            return result
        if x_github_event != "check_suite":
            return {"ignored": f"event {x_github_event}"}
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
        return await handle_deploy_event(deps_.repo, event)

    return app


async def fetch_spec(deps: ApiDeps, spec_ref: dict[str, str]) -> dict[str, Any]:
    """그 커밋의 deploy.yaml → 형식 검사를 통과한 명세. 없으면 SpecNotFound, 비었거나 깨졌으면 SpecProblem."""
    try:
        raw = await deps.specs.get_file(spec_ref["repository"], spec_ref["path"], spec_ref["commit"])
    except SpecNotFound:
        raise
    except GitHubError as exc:
        raise HTTPException(502, str(exc)) from exc
    return load_spec(raw, spec_ref["path"])


async def start_review(deps: ApiDeps, spec_ref: dict[str, str], requested_by: str) -> str:
    """POST /reviews — 파일이 없으면 SpecNotFound(404), 비었거나 깨졌으면 422."""
    try:
        loaded = await fetch_spec(deps, spec_ref)
    except SpecProblem as exc:
        raise HTTPException(422, {"message": exc.message, "errors": exc.errors}) from exc
    return await submit_review(deps, loaded, spec_ref, requested_by)


async def submit_review(deps: ApiDeps, loaded: dict[str, Any], spec_ref: dict[str, str], requested_by: str, *,
                        pr_number: int | None = None, generated_spec: bool = False) -> str:
    """review_id 발급 → DB received → review.requested 발행. review_id 반환."""
    commit = spec_ref["commit"]
    review_id = new_review_id()
    message = build_review_requested(loaded, review_id=review_id, spec_ref=spec_ref, requested_by=requested_by,
                                     requested_at=datetime.now(UTC), generated_spec=generated_spec)
    await deps.repo.insert_review(review_id=review_id, app=message.app, target_env=message.target_env,
                                  repo_id=message.repo_id, spec_ref=spec_ref, pr_head_sha=commit,
                                  requested_by=requested_by, pr_number=pr_number)
    try:
        await deps.publisher.send(REQUESTED_TOPIC, message.repo_id, message.encode())
    except Exception as exc:
        log.exception("review %s: review.requested 발행 실패", review_id)
        await deps.repo.update_review(review_id, status="failed", error=f"publish: {exc}"[:2000])
        raise HTTPException(503, "검토 요청을 큐에 넣지 못했다") from exc
    return review_id


PR_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


async def on_pull_request(deps: ApiDeps, payload: dict[str, Any]) -> dict[str, Any]:
    """기본 브랜치로 가는 PR 이 열리거나 커밋이 올라오면 그 head SHA 의 deploy.yaml 을 검토한다.

    - 같은 레포·같은 head SHA 검토가 이미 있으면 새로 만들지 않는다. 워커 commit_fix 가 PR 브랜치에 커밋하면
      synchronize 가 다시 오는데, 워커가 브랜치를 옮기기 전에 autofix_commit 검토를 DB 에 넣어 둔다
    - 새 검토를 만들면 그 PR 의 끝나지 않은 이전 검토는 superseded 로 넘긴다
    - deploy.yaml 이 없거나 비었거나 깨졌으면 검토 대신 intake 를 연다 (review_api.intake). 처리는 응답 뒤
    - intake 가 만든 생성 커밋에 온 웹훅이면 새 검토를 그 intake 에 잇는다
    - 그 PR 에 intake 가 baseline 없이 만든 명세 커밋이 있으면 generated_spec 으로 보낸다 → pass 여도 needs_human.
      생성 커밋 위에 커밋이 더 올라와도 같다(명세는 여전히 레포 분석으로 만든 것). 판단 근거는 spec_intakes 기록뿐이다 —
      명세 파일 내용·커밋 메시지·PR 작성자처럼 PR 을 올린 사람이 바꿀 수 있는 표시는 보지 않는다
    """
    action = payload.get("action")
    if action not in PR_ACTIONS:
        return {"ignored": f"action {action}"}
    pr, repo = payload["pull_request"], payload["repository"]
    if pr["base"]["ref"] != repo["default_branch"]:
        return {"ignored": f"base {pr['base']['ref']}"}
    repository, head_sha, number = repo["full_name"], pr["head"]["sha"], pr["number"]
    existing = await deps.repo.find_by_head(repository, head_sha)
    if existing is not None:
        return {"skipped": "already reviewed", "review_id": existing["review_id"]}
    taken = await deps.repo.find_intake_by_head(repository, head_sha)
    if taken is not None:
        return {"skipped": "already taken", "intake_id": taken["intake_id"]}
    spec_ref = {"repository": repository, "commit": head_sha, "path": "deploy.yaml"}
    try:
        loaded = await fetch_spec(deps, spec_ref)
    except SpecNotFound:
        if not await expects_spec(deps, repository):  # 조직 웹훅 — 배포 대상이 아닌 레포의 PR
            return {"skipped": "no deploy.yaml"}
        return await open_intake(deps, payload, SpecProblem("missing", "deploy.yaml 이 없다"), spec_ref["path"])
    except SpecProblem as problem:
        return await open_intake(deps, payload, problem, spec_ref["path"])
    generated = await deps.repo.unverified_generation_for_pr(repository, number)
    review_id = await submit_review(deps, loaded, spec_ref, payload["sender"]["login"], pr_number=number,
                                    generated_spec=generated is not None)
    superseded = await deps.repo.supersede_open(repository=repository, pr_number=number, superseded_by=review_id)
    result: dict[str, Any] = {"review_id": review_id, "superseded": superseded}
    if generated is not None:
        result["generated_spec"] = generated["intake_id"]
    source = await deps.repo.find_intake_by_result_commit(repository, head_sha)
    if source is not None:
        await deps.repo.link_intake(source["intake_id"], review_id=review_id)
        result["from_intake"] = source["intake_id"]
    return result


# 기본값(10분·재시도 2번)이면 처리 중인 intake 를 STALE_AFTER(2분) 뒤 sweep 이 다시 가져가 두 번 처리한다
REPAIR_CLIENT_OPTIONS: dict[str, Any] = {"timeout": 45.0, "max_retries": 1}


# 파일 전체를 다시 쓰는 출력이라 1분 남짓 걸린다. 재시도 1번까지 STALE_AFTER(5분) 안에 끝나게
TRANSFORM_CLIENT_OPTIONS: dict[str, Any] = {"timeout": 120.0, "max_retries": 1}


def make_transform_llm() -> LlmClient | None:
    """새 앱 코드 패치용 Claude. 출력 스키마만 TransformOutput."""
    try:
        return CachedLLM(ClaudeLLM(output=TransformOutput, client_options=TRANSFORM_CLIENT_OPTIONS,
                                   max_tokens=TRANSFORM_MAX_TOKENS))
    except LlmUnavailable as exc:
        log.warning("코드 패치 LLM 없이 시작 — 코드 패치가 필요한 새 앱은 TRANSFORM_UNAVAILABLE 로 거절한다: %s", exc)
        return None


def make_repair_llm() -> LlmClient | None:
    """형식 오류 명세 복구용 Claude. 워커 judge 와 같은 키·모델, 출력 스키마만 RepairOutput."""
    try:
        return CachedLLM(ClaudeLLM(output=RepairOutput, client_options=REPAIR_CLIENT_OPTIONS))
    except LlmUnavailable as exc:
        log.warning("명세 복구 LLM 없이 시작 — 형식 오류 명세는 REPAIR_UNAVAILABLE 로 거절한다: %s", exc)
        return None


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
        github=github,
        default_target=os.environ.get("DEFAULT_TARGET") or None,
        public_url=os.environ.get("REVIEW_API_PUBLIC_URL") or None,
        intake_repositories=frozenset(r.strip() for r in os.environ.get("INTAKE_REPOSITORIES", "").split(",")
                                      if r.strip()),
        repair_llm=make_repair_llm(),
        transform_llm=make_transform_llm(),
    )
    sweep = asyncio.create_task(sweep_stale_intakes(app.state.deps))
    try:
        yield
    finally:
        sweep.cancel()
        await producer.stop()
        await github.aclose()
        await pool.close()


app = create_app()
