"""멈춘 검토 회수 (review sweep) — intake 의 sweep_stale_intakes 와 같은 모양으로 API lifespan 에서 1분마다 돈다.

워커가 처리 중이어야 하는 상태(received·reviewing·merging)가 REVIEW_STALE_AFTER(기본 10분) 넘게 그대로면
워커가 메시지를 잃었거나 처리 중에 죽은 것이다. 한 행씩 조건부 UPDATE 로 가져가(recover_count +1) 다시 보낸다.
API 파드가 여럿이어도 한 행은 한 곳만 가져간다.

    received                       → review.requested 다시 발행 (spec_ref 커밋의 deploy.yaml 로 메시지를 다시 만든다)
    reviewing                      → received 로 되돌리고 다시 발행. 워커가 체크포인트를 보고 이어서 하거나 처음부터 한다
    merging, merge_sha 있음        → review.resumed(retry_overlay) — 워커가 commit_overlay 만 다시 한다
    merging, merge_sha 없음        → GitHub 에서 PR 이 병합됐는지 본다
                                       병합됨   → merge_sha 를 기록하고 retry_overlay
                                       아님     → waiting_ci 로 되돌리고 ci_completed — 워커가 CI 를 다시 조회한다
    recover_count > MAX_RECOVERIES → failed, 커밋 상태 review-service/verify failure

needs_human·waiting_ci 는 사람·CI 를 기다리는 상태라 회수하지 않는다.

REVIEW_STALE_AFTER 는 judge 최악(120초 × 4회 + 대기 ≈ 8분)보다 길어야 한다 — 짧으면 처리 중인 검토를 다시 보낸다.
같은 검토 메시지가 두 번 가도 워커 claim 이 하나만 진행시킨다.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from review_ai.messages import TOPIC as REQUESTED_TOPIC
from review_ai.messages import build_review_requested
from review_api import metrics
from review_api.intake import SpecProblem, load_spec
from review_common.github import SpecNotFound
from review_common.repository import RECOVERABLE
from review_common.resumed import TOPIC as RESUMED_TOPIC
from review_common.resumed import CiCompletedResumed, CiResult, RetryOverlayResumed

if TYPE_CHECKING:
    from review_api.app import ApiDeps

log = logging.getLogger(__name__)

DEFAULT_STALE_AFTER = timedelta(minutes=10)
SWEEP_EVERY_SECONDS = 60.0
MAX_RECOVERIES = 3


def stale_after() -> timedelta:
    """REVIEW_STALE_AFTER(초). 기본 10분."""
    raw = os.environ.get("REVIEW_STALE_AFTER")
    return timedelta(seconds=float(raw)) if raw else DEFAULT_STALE_AFTER


async def recover_stale_reviews(deps: ApiDeps, older_than: timedelta | None = None) -> list[dict[str, str]]:
    """멈춘 검토를 하나씩 가져가 회수한다. [{review_id, status(가져갈 때), action}]."""
    older_than = older_than or stale_after()
    done = []
    while (row := await deps.repo.claim_stale_review(older_than, RECOVERABLE)) is not None:
        metrics.safe(lambda: metrics.SWEEP_RECOVERED.labels(row["status"]).inc())
        try:
            action = await recover_review(deps, row)
        except Exception:  # 한 행이 실패해도 나머지는 회수한다 — 이 행은 다음 sweep 이 다시 가져간다
            log.exception("review %s: 회수 실패 (%s)", row["review_id"], row["status"])
            action = "error"
        log.info("review %s: 회수 %d/%d %s → %s", row["review_id"], row["recover_count"], MAX_RECOVERIES,
                 row["status"], action)
        done.append({"review_id": row["review_id"], "status": row["status"], "action": action})
    return done


async def recover_review(deps: ApiDeps, row: dict[str, Any]) -> str:
    rid, status = row["review_id"], row["status"]
    if row["recover_count"] > MAX_RECOVERIES:
        action = await _give_up(deps, row, f"멈춘 검토를 {MAX_RECOVERIES}번 회수해도 끝나지 않았다 (마지막 상태 {status})")
        if action == "failed":
            metrics.safe(metrics.SWEEP_FAILED.inc)
        return action
    if status == "received":
        return await _republish(deps, row)
    if status == "reviewing":
        if not await deps.repo.claim(rid, from_statuses=["reviewing"], to_status="received"):
            return "skipped"
        return await _republish(deps, row)
    if status == "merging":
        if row["gitops_commit_sha"]:
            return "skipped"
        if not row["merge_sha"]:
            merge_sha = await _merged_sha(deps, row)
            if merge_sha is None:
                return await _back_to_waiting_ci(deps, row)
            await deps.repo.update_review(rid, merge_sha=merge_sha)
        msg = RetryOverlayResumed(review_id=rid, resumed_at=datetime.now(UTC))
        await deps.publisher.send(RESUMED_TOPIC, rid, msg.model_dump_json().encode())
        return "retry_overlay"
    return "skipped"


async def _republish(deps: ApiDeps, row: dict[str, Any]) -> str:
    """spec_ref 커밋의 deploy.yaml 로 review.requested 를 다시 만든다. 처음 메시지와 review_id·spec_ref 가 같다.

    generated_spec 은 지금 기록(spec_intakes)으로 다시 정한다 — 사람이 이미 확인한 autofix 재검토라도 다시 묻는 쪽이다.
    """
    ref = row["spec_ref"]
    try:
        raw = await deps.specs.get_file(ref["repository"], ref["path"], ref["commit"])
        loaded = load_spec(raw, ref["path"])
    except (SpecNotFound, SpecProblem) as exc:  # 다시 해도 같다
        return await _give_up(deps, row, f"회수: 명세를 다시 읽을 수 없다 — {exc}")
    generated = row["pr_number"] is not None and (
        await deps.repo.unverified_generation_for_pr(ref["repository"], row["pr_number"])) is not None
    message = build_review_requested(loaded, review_id=row["review_id"], spec_ref=ref, requested_by=row["requested_by"],
                                     requested_at=row["created_at"],
                                     autofix_commit=row["requested_by"].startswith("autofix:"),
                                     generated_spec=generated)
    await deps.publisher.send(REQUESTED_TOPIC, message.repo_id, message.encode())
    return "requested"


async def _merged_sha(deps: ApiDeps, row: dict[str, Any]) -> str | None:
    """검토한 head 의 PR 이 이미 병합됐으면 병합 SHA. 아니면 None. GitHub 이 없거나 조회가 실패하면 예외(다음 sweep)."""
    if deps.github is None:
        raise RuntimeError("GitHub 클라이언트가 없어 병합 여부를 확인할 수 없다")
    ref = row["spec_ref"]
    for pull in await deps.github.pulls_for_commit(ref["repository"], ref["commit"]):  # type: ignore[attr-defined]
        if row["pr_number"] is not None and pull.get("number") != row["pr_number"]:
            continue
        if pull.get("merged_at") and pull.get("merge_commit_sha"):
            return pull["merge_commit_sha"]
    return None


async def _back_to_waiting_ci(deps: ApiDeps, row: dict[str, Any]) -> str:
    """병합 전에 멈췄다 — waiting_ci 로 되돌린다. CI 웹훅은 이미 지나갔을 수 있으니 ci_completed 를 같이 보낸다.

    워커는 웹훅 결론을 쓰지 않고 GitHub check-suites 를 다시 조회해 병합 여부를 정한다 (P1-5).
    """
    rid = row["review_id"]
    if not await deps.repo.claim(rid, from_statuses=["merging"], to_status="waiting_ci"):
        return "skipped"
    msg = CiCompletedResumed(review_id=rid, ci=CiResult(head_sha=row["pr_head_sha"], conclusion="unknown"),
                             resumed_at=datetime.now(UTC))
    await deps.publisher.send(RESUMED_TOPIC, rid, msg.model_dump_json().encode())
    return "waiting_ci"


async def _give_up(deps: ApiDeps, row: dict[str, Any], error: str) -> str:
    from review_api.app import post_verify_status  # app 이 이 모듈을 가져온다 — 순환을 피해 호출 때 가져온다

    rid = row["review_id"]
    if not await deps.repo.claim(rid, from_statuses=[row["status"]], to_status="failed"):
        return "skipped"
    await deps.repo.update_review(rid, error=error[:2000])
    await post_verify_status(deps, row["spec_ref"], rid, "failure", error)
    return "failed"


async def sweep_stale_reviews(deps: ApiDeps) -> None:
    """API 가 떠 있는 동안 recover_stale_reviews 를 주기적으로 돌린다."""
    while True:
        try:
            await recover_stale_reviews(deps)
        except Exception:
            log.exception("review sweep 실패")
        await asyncio.sleep(SWEEP_EVERY_SECONDS)
