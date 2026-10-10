"""Review API — 검토 요청을 받아 review.requested 를 발행하고, 사람 결정·CI·배포 결과를 받아 검토를 이어 간다.

    POST /reviews                       deploy.yaml 검토 요청 → 202 {review_id}  [Bearer REVIEW_API_TOKEN]
    GET  /reviews?status=..&limit=50    검토 목록, 최신순. status 는 여러 번 줄 수 있다 (사람 승인 화면)
    GET  /reviews/{review_id}           상태·verdict·사유·rounds·deploy_result
    GET  /reviews/{review_id}/progress  진행 화면용 — 검토 이력·단계·환경별 배포·링크 (review_api.progress)
    GET  /ui                            needs_human 승인 화면 (static/index.html). 토큰은 화면에서 입력받는다
    GET  /ui/reviews[/{review_id}]      진행 화면 (static/progress.html, 읽기 전용). PR 커밋 상태 Details 가 여기로 온다
    GET  /verify?sha=<PR head SHA>      그 SHA 의 검토가 통과했는지 (CI 용). 명세가 없거나 깨진 SHA 는 intake 상태
    GET  /intakes/{intake_id}           명세 없음·빈 명세·형식 오류 처리 기록 (PR 커밋 상태의 링크)
    POST /reviews/{review_id}/decision  needs_human 검토에 사람 결정 → review.resumed(human_decision)  [Bearer REVIEW_API_TOKEN]
    POST /webhooks/github               pull_request opened·synchronize·reopened → 검토 시작
                                        closed(병합 없이) → 그 PR 의 끝나지 않은 검토 superseded
                                        (deploy.yaml 없음·빈 파일·형식 오류 → spec_intakes, review_api.intake)
                                        check_suite completed → review.resumed(ci_completed)
    POST /webhooks/argocd               배포 Healthy·Degraded 기록, Healthy 면 baselines 갱신, Degraded 면 판단 사례
                                        [Bearer ARGOCD_WEBHOOK_TOKEN]
    GET  /healthz                       프로세스가 떠 있는지만 (ALB 헬스체크·livenessProbe)
    GET  /readyz                        DB SELECT 1·Kafka producer 연결. 하나라도 실패하면 503 (readinessProbe)
    GET  /metrics                       Prometheus 지표 (review_api.metrics). 토큰 없이

토큰이 설정되지 않은 [Bearer] 경로는 503 으로 거절한다 (fail-closed). GET 경로와 /webhooks/github(서명 검증)는 그대로.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, get_args

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_ai.graph import check_edited_ops
from review_ai.intake.repair import RepairOutput
from review_ai.judge.llm import CachedLLM, ClaudeLLM, LlmClient, LlmUnavailable
from review_ai.messages import TOPIC as REQUESTED_TOPIC
from review_ai.messages import build_review_requested
from review_ai.recommendations import resolve_human_decision
from review_ai.spec.deploy_spec import REPOSITORY
from review_ai.transform import TRANSFORM_MAX_TOKENS
from review_ai.transform.prompt import TransformOutput
from review_api.argocd import ArgoCdEvent, handle_deploy_event
from review_api.deployment_contract import MAX_BODY
from review_api.deployments import ResolutionIn, resolve_case
from review_ai.failure_evidence import safe_data
from review_common.deployment import dispatch_best_effort, sweep_analysis
from review_api.intake import (IntakeGitHub, SpecProblem, expects_spec, load_spec, open_intake, process_intake,
                               sweep_stale_intakes, unverified_of)
from review_api import metrics
from review_api.lockfile import regenerate_lockfile
from review_api.progress import ProgressSettings, build_progress, deployment_summary, review_view
from review_api.recovery import sweep_stale_reviews
from review_common.github import GitHubError, SpecNotFound
from review_common.ids import new_review_id
from review_common.repository import ReviewDbStatus, ReviewRepository
from review_common.resumed import TOPIC as RESUMED_TOPIC
from review_common.resumed import CiCompletedResumed, CiResult, HumanDecisionModel, HumanDecisionResumed

log = logging.getLogger(__name__)

PASSED_STATUSES = frozenset({"waiting_ci", "merging", "committed"})  # 검토를 통과(사람 승인 포함)한 뒤의 상태
INTAKE_FAILED = frozenset({"rejected", "failed"})
REVIEW_STATUSES = frozenset(get_args(ReviewDbStatus))

UI_PAGE = Path(__file__).parent / "static" / "index.html"
PROGRESS_PAGE = Path(__file__).parent / "static" / "progress.html"
# 화면은 같은 주소의 API 만 부른다. 인라인 스크립트·스타일 한 장이라 외부 리소스는 막는다
UI_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';"
                               " connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self';"
                               " frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


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
    api_token: str | None = None  # REVIEW_API_TOKEN — POST /reviews·/decision. 비어 있으면 두 경로를 503 으로 거절
    ci_app_slug: str | None = "github-actions"  # 이 GitHub App 의 check_suite 만 CI 결과로 본다. None 이면 전부
    github: IntakeGitHub | None = None  # 명세 생성 커밋·PR 커밋 상태. None 이면 intake 는 기록만 하고 거절
    default_target: str | None = None   # "aws/ap-northeast-2" — baseline 없는 레포의 명세를 만들 대상
    public_url: str | None = None       # PR 커밋 상태 링크(/intakes/{id}·/ui/reviews/{id})의 앞부분
    intake_repositories: frozenset[str] = frozenset()  # baseline 이 없어도 deploy.yaml 없음을 intake 로 볼 레포
    repair_llm: LlmClient | None = None  # 형식 오류 명세 복구. None 이면 yaml_error·schema_error 는 REPAIR_UNAVAILABLE
    transform_llm: LlmClient | None = None  # 새 앱 코드 패치(SQLite→Postgres·/metrics). None 이면 TRANSFORM_UNAVAILABLE
    lockfile: Callable[[str, str], Awaitable[str]] = regenerate_lockfile  # 코드 패치가 바꾼 의존성의 잠금 파일
    # /readyz 가 볼 의존성 — 이름 → 실패하면 예외를 내는 확인. 비어 있으면 늘 준비됨
    readiness: dict[str, Callable[[], Awaitable[Any]]] = field(default_factory=dict)
    progress: ProgressSettings = field(default_factory=ProgressSettings)  # DEPLOY_ENVS·PLANNED_ENVS·APP_URLS·GRAFANA_URL


READY_TIMEOUT_SECONDS = 2.0  # 확인 하나의 상한. readinessProbe timeoutSeconds 는 이보다 길게 (gitops, 기본 1초)


def make_readiness(pool: Any, producer: Any) -> dict[str, Callable[[], Awaitable[Any]]]:
    """운영 /readyz 확인 — 업무 DB SELECT 1, Kafka 브로커와 메타데이터 왕복(끊겼으면 KafkaError)."""
    async def db() -> None:
        async with pool.connection(timeout=READY_TIMEOUT_SECONDS) as conn:
            await conn.execute("SELECT 1")

    async def kafka() -> None:
        await producer.client.fetch_all_metadata()

    return {"db": db, "kafka": kafka}


async def check_ready(checks: dict[str, Callable[[], Awaitable[Any]]]) -> dict[str, str]:
    """각 확인을 동시에, READY_TIMEOUT_SECONDS 안에. 이름 → "ok" 또는 실패 사유."""
    async def one(check: Callable[[], Awaitable[Any]]) -> str:
        try:
            await asyncio.wait_for(check(), READY_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — 사유만 보여 준다
            return f"{type(exc).__name__}: {exc}"[:200] if str(exc) else type(exc).__name__
        return "ok"

    results = await asyncio.gather(*(one(check) for check in checks.values()))
    return dict(zip(checks, results, strict=True))


class SpecRefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(pattern=REPOSITORY, description="owner/repo")
    commit: str = Field(pattern=r"^[0-9a-f]{40}$", description="PR head SHA (40자). 병합은 전체 SHA 가 필요하다")
    path: str = Field(default="deploy.yaml", min_length=1)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec_ref: SpecRefIn
    requested_by: str = Field(min_length=1)


def require_bearer(authorization: str, token: str | None, name: str) -> None:
    """Authorization: Bearer <token> 확인. 토큰이 설정되지 않았으면 503 — 비어 있다고 검사를 건너뛰지 않는다(fail-closed).

    플랫폼 ALB 가 HTTP 로 열려 있어서, 토큰이 없으면 인터넷 누구나 needs_human 검토를 승인할 수 있다.
    """
    if not token:
        raise HTTPException(503, f"{name} 이 설정되지 않았다")
    if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "토큰이 맞지 않다")


def create_app(deps: ApiDeps | None = None) -> FastAPI:
    lifespan = None if deps is not None else _real_lifespan
    app = FastAPI(title="review-service", lifespan=lifespan)
    if deps is not None:
        app.state.deps = deps

    def d(request: Request) -> ApiDeps:
        return request.app.state.deps

    @app.middleware("http")
    async def http_metrics(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        started, status = time.perf_counter(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", None) or "unmatched"  # 경로 템플릿 — 실제 ID 를 넣지 않는다
            metrics.observe_http(route, request.method, status, time.perf_counter() - started)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(request: Request) -> Response:
        body, content_type = await metrics.render(d(request).repo)
        return Response(body, media_type=content_type)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        checks = await check_ready(d(request).readiness)
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ok" if ready else "unavailable", "checks": checks},
                            status_code=200 if ready else 503)

    @app.post("/reviews", status_code=202)
    async def create_review(body: ReviewIn, request: Request,
                            authorization: str = Header(default="")) -> dict[str, str]:
        require_bearer(authorization, d(request).api_token, "REVIEW_API_TOKEN")
        try:
            review_id = await start_review(d(request), body.spec_ref.model_dump(), body.requested_by)
        except SpecNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"review_id": review_id}

    @app.get("/reviews")
    async def list_reviews(request: Request, status: list[str] = Query(default=[]),
                           limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
        unknown = sorted(set(status) - REVIEW_STATUSES)
        if unknown:
            raise HTTPException(422, f"모르는 status: {', '.join(unknown)}")
        return await d(request).repo.list_reviews(status, limit)

    @app.get("/ui", include_in_schema=False)
    async def ui() -> FileResponse:
        return FileResponse(UI_PAGE, media_type="text/html; charset=utf-8", headers=UI_HEADERS)

    @app.get("/ui/reviews", include_in_schema=False)
    @app.get("/ui/reviews/{review_id}", include_in_schema=False)
    async def progress_ui() -> FileResponse:
        return FileResponse(PROGRESS_PAGE, media_type="text/html; charset=utf-8", headers=UI_HEADERS)

    @app.get("/reviews/{review_id}")
    async def get_review(review_id: str, request: Request) -> dict[str, Any]:
        row = await d(request).repo.get_review(review_id)
        if row is None:
            raise HTTPException(404, "검토가 없다")
        deployment = await deployment_summary(d(request).repo, row)
        return {**review_view(row), "deployment": deployment,
                "case_advice_status": (row.get("case_advice") or {}).get("status", "not_checked")}

    @app.get("/reviews/{review_id}/progress")
    async def get_progress(review_id: str, request: Request) -> dict[str, Any]:
        progress = await build_progress(d(request).repo, review_id, d(request).progress)
        if progress is None:
            raise HTTPException(404, "검토가 없다")
        return progress

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
    async def decide(review_id: str, body: HumanDecisionModel, request: Request,
                     authorization: str = Header(default="")) -> dict[str, str]:
        deps_ = d(request)
        require_bearer(authorization, deps_.api_token, "REVIEW_API_TOKEN")
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
        await publish(deps_, RESUMED_TOPIC, review_id, msg.model_dump_json().encode())
        return {"review_id": review_id}

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(
        request: Request,
        background: BackgroundTasks,
        x_github_event: str = Header(default=""),
        x_hub_signature_256: str = Header(default=""),
    ) -> dict[str, Any]:
        event = x_github_event if x_github_event in ("pull_request", "check_suite") else "other"
        try:
            result = await _github_webhook(request, background, x_github_event, x_hub_signature_256)
        except HTTPException as exc:
            metrics.webhook(event, "rejected" if exc.status_code in (401, 503) and exc.detail != PUBLISH_FAILED
                            else "error")
            raise
        except Exception:
            metrics.webhook(event, "error")
            raise
        if event == "pull_request":
            metrics.webhook(event, metrics.pull_request_result(result))
        else:
            metrics.webhook(event, "started" if result.get("resumed") else "skipped")
        return result

    async def _github_webhook(request: Request, background: BackgroundTasks, x_github_event: str,
                              x_hub_signature_256: str) -> dict[str, Any]:
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
            await publish(deps_, RESUMED_TOPIC, row["review_id"], msg.model_dump_json().encode())
            resumed.append(row["review_id"])
        return {"resumed": resumed}

    @app.post("/webhooks/argocd", status_code=202)
    async def argocd_webhook(request: Request, background: BackgroundTasks,
                             authorization: str = Header(default="")) -> dict[str, Any]:
        deps_ = d(request)
        try:
            require_bearer(authorization, deps_.argocd_webhook_token, "ARGOCD_WEBHOOK_TOKEN")
        except HTTPException:
            metrics.webhook("argocd", "rejected")
            raise
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BODY:
                raise HTTPException(413, "웹훅 본문이 너무 큽니다")
        try:
            event = ArgoCdEvent.model_validate_json(bytes(data))
        except ValidationError:
            # Pydantic error input may contain credentials; never echo it to the sender.
            raise HTTPException(422, "배포 결과 JSON 명세가 맞지 않습니다")
        try:
            result = await handle_deploy_event(deps_.repo, event)
        except Exception:
            metrics.webhook("argocd", "error")
            raise
        metrics.webhook("argocd", metrics.argocd_result(result))
        linked = result.get("linked", bool(result.get("review_id")) and not result.get("cross_env"))
        log.info("argocd 웹훅 event_id=%s kind=%s app=%s env=%s review_id=%s linked=%s%s",
                 result.get("event_id"), event.kind(), event.app, event.env, result.get("review_id"), linked,
                 " duplicate" if result.get("duplicate") else "")
        background.add_task(dispatch_best_effort, deps_.repo, deps_.publisher)
        return result

    @app.get("/deployments")
    async def deployments(request: Request, authorization: str = Header(default=""),
                          review_id: str | None = None, limit: int = Query(default=50, ge=1, le=100)):
        require_bearer(authorization, d(request).api_token, "REVIEW_API_TOKEN")
        return safe_data(await d(request).repo.list_deployments(review_id, limit))

    @app.get("/deployments/{event_id}")
    async def deployment(event_id: str, request: Request, authorization: str = Header(default="")):
        require_bearer(authorization, d(request).api_token, "REVIEW_API_TOKEN")
        row = await d(request).repo.get_deployment(event_id)
        if row is None:
            raise HTTPException(404, "배포 결과가 없습니다")
        return safe_data(row)

    @app.get("/reviews/{review_id}/case-advice")
    async def advice(review_id: str, request: Request, authorization: str = Header(default="")):
        require_bearer(authorization, d(request).api_token, "REVIEW_API_TOKEN")
        row = await d(request).repo.get_review(review_id)
        if row is None:
            raise HTTPException(404, "검토가 없습니다")
        return safe_data(row.get("case_advice") or {"status": "not_checked", "items": []})

    @app.post("/failure-cases/{case_id}/resolution")
    async def resolution(case_id: str, body: ResolutionIn, request: Request,
                         authorization: str = Header(default="")):
        require_bearer(authorization, d(request).api_token, "REVIEW_API_TOKEN")
        return await resolve_case(d(request).repo, case_id, body)

    return app


PUBLISH_FAILED = "메시지를 큐에 넣지 못했다"


async def publish(deps: ApiDeps, topic: str, key: str, value: bytes) -> None:
    """발행 실패·시간 초과(KAFKA_SEND_TIMEOUT)는 503 — 웹훅 응답을 붙잡지 않는다. GitHub 은 실패한 전달을 다시 보낼 수 있다."""
    try:
        await deps.publisher.send(topic, key, value)
    except Exception as exc:
        log.exception("%s %s 발행 실패", topic, key)
        raise HTTPException(503, PUBLISH_FAILED) from exc


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
                        pr_number: int | None = None, generated_spec: bool = False,
                        unverified_paths: Sequence[str] = ()) -> str:
    """review_id 발급 → DB received → review.requested 발행. review_id 반환.

    같은 레포·head SHA 검토가 이미 있으면(동시에 온 같은 웹훅) 새로 발행하지 않고 그 review_id 를 돌려준다."""
    commit = spec_ref["commit"]
    review_id = new_review_id()
    message = build_review_requested(loaded, review_id=review_id, spec_ref=spec_ref, requested_by=requested_by,
                                     requested_at=datetime.now(UTC), generated_spec=generated_spec,
                                     unverified_paths=unverified_paths)
    stored = await deps.repo.insert_review(review_id=review_id, app=message.app, target_env=message.target_env,
                                           repo_id=message.repo_id, spec_ref=spec_ref, pr_head_sha=commit,
                                           requested_by=requested_by, pr_number=pr_number)
    if stored != review_id:
        log.info("%s@%s: 검토 %s 가 이미 있다 — 새로 발행하지 않는다", message.repo_id, commit, stored)
        return stored
    try:
        await deps.publisher.send(REQUESTED_TOPIC, message.repo_id, message.encode())
    except Exception as exc:
        log.exception("review %s: review.requested 발행 실패", review_id)
        await deps.repo.update_review(review_id, status="failed", error=f"publish: {exc}"[:2000])
        raise HTTPException(503, PUBLISH_FAILED) from exc
    await post_verify_status(deps, spec_ref, review_id, "pending", "AI 검토 중")
    return review_id


VERIFY_CONTEXT = "review-service/verify"  # 검토 결과 커밋 상태. 이후 상태(success·failure)는 워커가 쓴다


async def post_verify_status(deps: ApiDeps, spec_ref: dict[str, str], review_id: str, state: str,
                             description: str) -> None:
    """PR head 에 커밋 상태 review-service/verify. 실패해도(권한·네트워크) 검토는 그대로 진행한다."""
    if deps.github is None:
        return
    target_url = f"{deps.public_url.rstrip('/')}/ui/reviews/{review_id}" if deps.public_url else None  # 진행 화면
    try:
        await deps.github.create_commit_status(spec_ref["repository"], spec_ref["commit"], state=state,
                                               context=VERIFY_CONTEXT, description=description,
                                               target_url=target_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("review %s: 커밋 상태(%s) 기록 실패 — %s", review_id, state, exc)


def is_fork(pull_request: dict[str, Any]) -> bool:
    """head 가 base 와 다른 레포(포크)이거나 head 레포가 없으면(포크가 지워짐) True."""
    head_repo = (pull_request.get("head") or {}).get("repo")
    base_repo = (pull_request.get("base") or {}).get("repo") or {}
    return head_repo is None or head_repo.get("full_name") != base_repo.get("full_name")


PR_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


async def on_pull_request(deps: ApiDeps, payload: dict[str, Any]) -> dict[str, Any]:
    """기본 브랜치로 가는 PR 이 열리거나 커밋이 올라오면 그 head SHA 의 deploy.yaml 을 검토한다.

    - 같은 레포·같은 head SHA 검토가 이미 있으면 새로 만들지 않는다. 워커 commit_fix 가 PR 브랜치에 커밋하면
      synchronize 가 다시 오는데, 워커가 브랜치를 옮기기 전에 autofix_commit 검토를 DB 에 넣어 둔다
    - 새 검토를 만들면 그 PR 의 끝나지 않은 이전 검토는 superseded 로 넘긴다
    - deploy.yaml 이 없거나 비었거나 깨졌으면 검토 대신 intake 를 연다 (review_api.intake). 처리는 응답 뒤
    - intake 가 만든 생성 커밋에 온 웹훅이면 새 검토를 그 intake 에 잇는다
    - 그 PR 에 intake 가 baseline 없이, 확인하지 못한 값을 후보값으로 채워 만든 명세 커밋이 있으면 generated_spec 으로
      보낸다(그 경로는 unverified_paths) → pass 여도 needs_human. 전부 확인된 생성 명세는 일반 검토와 같다.
      생성 커밋 위에 커밋이 더 올라와도 같다(명세는 여전히 레포 분석으로 만든 것). 판단 근거는 spec_intakes 기록뿐이다 —
      명세 파일 내용·커밋 메시지·PR 작성자처럼 PR 을 올린 사람이 바꿀 수 있는 표시는 보지 않는다
    - 포크 PR 은 {"skipped": "fork"} — 검토·intake 모두 하지 않는다
    - 병합 없이 닫힌 PR(closed, merged=false)은 끝나지 않은 검토를 superseded(error "PR closed")로 넘긴다 →
      승인 화면 목록에서 빠진다. 체크포인터에 멈춘 그래프는 그대로 둔다 — 상태가 바뀌어 재개 claim 이 실패한다.
      다시 열면(reopened) 같은 head SHA 라도 새로 검토한다
    """
    action = payload.get("action")
    if action == "closed":
        return await on_pull_request_closed(deps, payload)
    if action not in PR_ACTIONS:
        return {"ignored": f"action {action}"}
    pr, repo = payload["pull_request"], payload["repository"]
    if pr["base"]["ref"] != repo["default_branch"]:
        return {"ignored": f"base {pr['base']['ref']}"}
    if is_fork(pr):  # 포크 브랜치는 쓸 수도(수정 커밋) 믿을 수도 없다 — 검토도 intake 도 하지 않는다
        return {"skipped": "fork"}
    repository, head_sha, number = repo["full_name"], pr["head"]["sha"], pr["number"]
    existing = await deps.repo.find_by_head(repository, head_sha)
    if existing is not None and not (action == "reopened" and existing["status"] == "superseded"):
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
                                    generated_spec=generated is not None, unverified_paths=unverified_of(generated))
    superseded = await deps.repo.supersede_open(repository=repository, pr_number=number, superseded_by=review_id)
    result: dict[str, Any] = {"review_id": review_id, "superseded": superseded}
    if generated is not None:
        result["generated_spec"] = generated["intake_id"]
    source = await deps.repo.find_intake_by_result_commit(repository, head_sha)
    if source is not None:
        await deps.repo.link_intake(source["intake_id"], review_id=review_id)
        result["from_intake"] = source["intake_id"]
    return result


PR_CLOSED = "PR closed"


async def on_pull_request_closed(deps: ApiDeps, payload: dict[str, Any]) -> dict[str, Any]:
    """병합 없이 닫힌 PR — 그 PR 의 끝나지 않은 검토(received·reviewing·needs_human·waiting_ci)를 superseded 로.

    병합으로 닫힌 것(워커 merge_pr 이 병합했다)은 아무것도 하지 않는다 — 검토는 merging 이후 상태라 OPEN 에도 없다.
    """
    pr = payload["pull_request"]
    if pr.get("merged"):
        return {"ignored": "merged"}
    superseded = await deps.repo.supersede_open(repository=payload["repository"]["full_name"],
                                                pr_number=pr["number"], superseded_by=None, error=PR_CLOSED)
    return {"closed": pr["number"], "superseded": superseded}


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

    # uvicorn 은 자기 로거만 설정한다 — 루트가 없으면 review_api 의 INFO(웹훅 linked 등)가 안 나온다. 워커와 같은 형식
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from aiokafka import AIOKafkaProducer

    from review_common.github import GitHubClient
    from review_common.kafka import KafkaPublisher
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

    app.state.deps = ApiDeps(
        repo=PostgresReviewRepository(pool),
        specs=github,
        publisher=KafkaPublisher(producer),
        github_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET"),
        argocd_webhook_token=os.environ.get("ARGOCD_WEBHOOK_TOKEN") or None,
        api_token=os.environ.get("REVIEW_API_TOKEN") or None,
        ci_app_slug=os.environ.get("GITHUB_CI_APP_SLUG", "github-actions") or None,
        github=github,
        default_target=os.environ.get("DEFAULT_TARGET") or None,
        public_url=os.environ.get("REVIEW_API_PUBLIC_URL") or None,
        intake_repositories=frozenset(r.strip() for r in os.environ.get("INTAKE_REPOSITORIES", "").split(",")
                                      if r.strip()),
        repair_llm=make_repair_llm(),
        transform_llm=make_transform_llm(),
        readiness=make_readiness(pool, producer),
        progress=ProgressSettings.from_env(os.environ),
    )
    sweeps = [asyncio.create_task(sweep_stale_intakes(app.state.deps)),
              asyncio.create_task(sweep_stale_reviews(app.state.deps)),
              asyncio.create_task(sweep_analysis(app.state.deps.repo, app.state.deps.publisher))]  # 멈춘 intake·검토 회수
    try:
        yield
    finally:
        for sweep in sweeps:
            sweep.cancel()
        await asyncio.gather(*sweeps, return_exceptions=True)
        await producer.stop()
        await github.aclose()
        await pool.close()


app = create_app()
