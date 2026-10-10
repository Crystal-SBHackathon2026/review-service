"""Kafka 메시지 처리 — review.requested 로 그래프를 시작하고, review.resumed 로 interrupt 를 재개한다.

중복 방지: 업무 DB 상태를 조건부 UPDATE(claim) 로 넘긴다. 넘기지 못하면(이미 처리 중이거나 끝남) 건너뛴다.
그래프 안에서 난 예외는 DB status=failed 로 남기고 삼킨다 — 오프셋은 커밋되고 다음 메시지로 간다.
단 PR 을 병합한 뒤(merge_sha 있음) gitops 커밋에서 난 예외는 merging 으로 둔다 — review sweep 이 commit_overlay 만 다시 한다.

멈춘 검토 회수(Review API review sweep)가 같은 메시지를 다시 보낸다. 그래서 시작·재개 전에 체크포인터를 본다.
    review.requested — 체크포인트가 없거나 끝났으면 처음부터, 중간에 멈췄으면 거기서 이어서,
                       사람 결정 대기(interrupt)에 멈춰 있으면 needs_human 으로 되돌린다
    ci_completed     — CI 대기(interrupt)가 아니면 CI 확인(await_ci)부터 다시 한다
    retry_overlay    — commit_overlay 만 다시 한다
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import Command
from pydantic import ValidationError

from review_ai.graph import initial_state
from review_ai.messages import TOPIC as REQUESTED_TOPIC
from review_ai.messages import ReviewRequested
from review_common.repository import ReviewRepository, baseline_for
from review_common.resumed import TOPIC as RESUMED_TOPIC
from review_common.resumed import CiCompletedResumed, HumanDecisionResumed, RetryOverlayResumed, parse_review_resumed
from review_worker import metrics
from review_worker.graph import RECURSION_LIMIT
from review_worker.observe import RepoFiles, observe_migrations

log = logging.getLogger(__name__)


def _config(review_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": review_id}, "recursion_limit": RECURSION_LIMIT}


def pending_interrupt(snapshot: Any) -> str | None:
    """체크포인트가 멈춰 있는 interrupt 의 kind (human_decision·ci_completed). interrupt 가 아니면 None."""
    for task in snapshot.tasks:
        for pending in task.interrupts:
            if isinstance(pending.value, dict) and pending.value.get("kind"):
                return pending.value["kind"]
    return None


class ReviewHandler:
    def __init__(self, repo: ReviewRepository, graph: Any, files: RepoFiles | None = None) -> None:
        """files 가 있으면 검토 전에 새 마이그레이션 SQL 을 판정해 deploy_spec.observed 에 넣는다(RUN-008)."""
        self._repo = repo
        self._graph = graph
        self._files = files

    async def handle(self, topic: str, value: bytes) -> None:
        """지표 review_worker_messages_total{topic,kind,result} — result: processed·skipped·invalid·error."""
        kind, result = "unknown", "error"
        try:
            if topic == REQUESTED_TOPIC:
                kind = "requested"
                processed = await self.on_requested(ReviewRequested.model_validate_json(value))
            elif topic == RESUMED_TOPIC:
                msg = parse_review_resumed(value)
                kind = msg.kind
                processed = await self.on_resumed(msg)
            else:
                log.warning("모르는 토픽 %s — 건너뜀", topic)
                processed = False
            result = "processed" if processed else "skipped"
        except ValidationError as exc:
            log.error("%s 메시지 형식 오류 — 건너뜀: %s", topic, exc)
            result = "invalid"
        finally:
            metrics.safe(lambda: metrics.MESSAGES.labels(topic, kind, result).inc())

    async def on_requested(self, msg: ReviewRequested) -> bool:
        """처리했으면 True, 건너뛰었으면(claim 실패) False."""
        rid = msg.review_id
        if not await self._repo.claim(rid, from_statuses=["received"], to_status="reviewing"):
            log.info("review %s: 이미 처리 중이거나 끝남 — 건너뜀", rid)
            return False
        snapshot = await self._graph.aget_state(_config(rid))
        waiting = pending_interrupt(snapshot)
        if waiting == "human_decision":  # 사람 결정을 받아 재개하다 멈췄다 — 결정은 다시 받는다
            log.info("review %s: 사람 결정 대기로 되돌린다 (회수)", rid)
            await self._repo.claim(rid, from_statuses=["reviewing"], to_status="needs_human")
            return True
        if waiting == "ci_completed":  # wait_ci 는 웹훅 결론을 쓰지 않고 GitHub 에서 다시 조회한다
            log.info("review %s: CI 대기에서 이어서 CI 를 다시 확인한다 (회수)", rid)
            await self._run(rid, Command(resume={"head_sha": msg.spec_ref.commit, "conclusion": "unknown"}))
            return True
        if snapshot.next:
            log.info("review %s: 체크포인트 %s 에서 이어서 한다 (회수)", rid, snapshot.next)
            await self._run(rid, None)
            return True
        spec = dict(msg.deploy_spec)
        row = await self._repo.baseline_or_last_committed(msg.app, msg.target_env)  # 웹훅 없는 local·gcp 대체
        baseline = baseline_for(row)
        if baseline is not None:
            spec["baseline"] = baseline
        if self._files is not None:
            observed = await observe_migrations(self._files, msg.spec_ref.repository, msg.spec_ref.commit, row)
            if observed is not None:
                spec["observed"] = observed
        await self._run(rid, initial_state(spec, review_id=rid, spec_ref=msg.spec_ref.model_dump(),
                                           autofix_commit=msg.autofix_commit, generated_spec=msg.generated_spec,
                                           unverified_paths=msg.unverified_paths))
        return True

    async def on_resumed(self, msg: HumanDecisionResumed | CiCompletedResumed | RetryOverlayResumed) -> bool:
        rid = msg.review_id
        if isinstance(msg, RetryOverlayResumed):
            return await self.on_retry_overlay(rid)
        if isinstance(msg, CiCompletedResumed):
            claimed = await self._repo.claim(rid, from_statuses=["waiting_ci"], to_status="merging",
                                             pr_head_sha=msg.ci.head_sha)
            value = msg.ci.model_dump(mode="json")
        else:
            claimed = await self._repo.claim(rid, from_statuses=["needs_human"], to_status="reviewing")
            value = msg.human_decision.model_dump(mode="json")
        if not claimed:
            log.info("review %s: %s 재개 대상 상태가 아님 — 건너뜀", rid, msg.kind)
            return False
        graph_input: Any = Command(resume=value)
        if isinstance(msg, CiCompletedResumed):
            snapshot = await self._graph.aget_state(_config(rid))
            waiting = pending_interrupt(snapshot)
            if waiting is None and snapshot.values:  # 병합 직전에 멈췄다가 회수된 검토 — CI 부터 다시 확인한다
                log.info("review %s: CI 대기 지점이 아니다 (%s) — CI 확인부터 다시 한다", rid, snapshot.next)
                graph_input = Command(goto="await_ci")
            elif waiting != "ci_completed":
                await self._fail(rid, f"CI 재개할 체크포인트가 없다 ({waiting})")
                return True
        await self._run(rid, graph_input)
        return True

    async def on_retry_overlay(self, rid: str) -> bool:
        """병합은 됐는데 gitops 커밋이 없는 검토 — 체크포인트의 State 로 commit_overlay 노드만 다시 돌린다."""
        row = await self._repo.get_review(rid)
        if row is None or row["status"] != "merging" or not row["merge_sha"] or row["gitops_commit_sha"]:
            log.info("review %s: retry_overlay 대상이 아님 — 건너뜀", rid)
            return False
        snapshot = await self._graph.aget_state(_config(rid))
        if not snapshot.values:
            await self._fail(rid, "병합은 됐지만 체크포인트가 없어 gitops 커밋을 다시 할 수 없다")
            return True
        log.info("review %s: commit_overlay 다시 (병합 %s)", rid, row["merge_sha"])
        await self._run(rid, Command(goto="commit_overlay"))
        return True

    async def _run(self, review_id: str, graph_input: Any) -> None:
        try:
            await self._graph.ainvoke(graph_input, _config(review_id))
        except Exception as exc:  # noqa: BLE001 — 버그·외부 장애는 검토를 failed 로 남기고 다음 메시지로
            log.exception("review %s: 그래프 실패", review_id)
            error = f"{type(exc).__name__}: {exc}"[:2000]
            row = await self._repo.get_review(review_id)
            if row and row["status"] == "merging" and row["merge_sha"] and not row["gitops_commit_sha"]:
                # main 에는 이미 병합됐다 — failed 로 끝내면 배포가 영영 안 된다. sweep 이 retry_overlay 를 보낸다
                await self._repo.update_review(review_id, error=error)
                return
            await self._fail(review_id, error, row)

    async def _fail(self, review_id: str, error: str, row: dict[str, Any] | None = None) -> None:
        await self._repo.update_review(review_id, status="failed", error=error)
        row = row or await self._repo.get_review(review_id)
        if row:
            metrics.finished(row["app"], row["target_env"], "failed")
