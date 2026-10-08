"""Kafka 메시지 처리 — review.requested 로 그래프를 시작하고, review.resumed 로 interrupt 를 재개한다.

중복 방지: 업무 DB 상태를 조건부 UPDATE(claim) 로 넘긴다. 넘기지 못하면(이미 처리 중이거나 끝남) 건너뛴다.
그래프 안에서 난 예외는 DB status=failed 로 남기고 삼킨다 — 오프셋은 커밋되고 다음 메시지로 간다.
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
from review_common.resumed import CiCompletedResumed, parse_review_resumed
from review_worker.graph import RECURSION_LIMIT

log = logging.getLogger(__name__)


class ReviewHandler:
    def __init__(self, repo: ReviewRepository, graph: Any) -> None:
        self._repo = repo
        self._graph = graph

    async def handle(self, topic: str, value: bytes) -> None:
        try:
            if topic == REQUESTED_TOPIC:
                await self.on_requested(ReviewRequested.model_validate_json(value))
            elif topic == RESUMED_TOPIC:
                await self.on_resumed(parse_review_resumed(value))
            else:
                log.warning("모르는 토픽 %s — 건너뜀", topic)
        except ValidationError as exc:
            log.error("%s 메시지 형식 오류 — 건너뜀: %s", topic, exc)

    async def on_requested(self, msg: ReviewRequested) -> None:
        rid = msg.review_id
        if not await self._repo.claim(rid, from_statuses=["received"], to_status="reviewing"):
            log.info("review %s: 이미 처리 중이거나 끝남 — 건너뜀", rid)
            return
        spec = dict(msg.deploy_spec)
        baseline = baseline_for(await self._repo.get_baseline(msg.app, msg.target_env))
        if baseline is not None:
            spec["baseline"] = baseline
        await self._run(rid, initial_state(spec, review_id=rid, spec_ref=msg.spec_ref.model_dump(),
                                           autofix_commit=msg.autofix_commit))

    async def on_resumed(self, msg: Any) -> None:
        rid = msg.review_id
        if isinstance(msg, CiCompletedResumed):
            claimed = await self._repo.claim(rid, from_statuses=["waiting_ci"], to_status="merging",
                                             pr_head_sha=msg.ci.head_sha)
            value = msg.ci.model_dump(mode="json")
        else:
            claimed = await self._repo.claim(rid, from_statuses=["needs_human"], to_status="reviewing")
            value = msg.human_decision.model_dump(mode="json")
        if not claimed:
            log.info("review %s: %s 재개 대상 상태가 아님 — 건너뜀", rid, msg.kind)
            return
        await self._run(rid, Command(resume=value))

    async def _run(self, review_id: str, graph_input: Any) -> None:
        config = {"configurable": {"thread_id": review_id}, "recursion_limit": RECURSION_LIMIT}
        try:
            await self._graph.ainvoke(graph_input, config)
        except Exception as exc:  # noqa: BLE001 — 버그·외부 장애는 검토를 failed 로 남기고 다음 메시지로
            log.exception("review %s: 그래프 실패", review_id)
            await self._repo.update_review(review_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:2000])
