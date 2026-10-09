"""워커 handler — review sweep 이 다시 보낸 메시지를 체크포인트를 보고 이어서 처리한다 (P1-4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from review_common.resumed import RetryOverlayResumed
from tests.conftest import HEAD, MERGE_SHA, Harness, load_sample, suite
from tests.test_graph import RID, SAMPLE_01, SAMPLE_04, _finish_ci, _seed_baseline, _state


def break_record_result_once(h: Harness) -> list[dict[str, Any]]:
    """record_result 의 DB 쓰기가 한 번 실패한다 (DB 순간 장애). 그때 handler 의 failed 기록도 실패해 프로세스가 내려간다."""
    original, calls = h.repo.update_review, []

    async def update_review(review_id: str, **fields: Any) -> None:
        calls.append(fields)
        if len(calls) <= 2 and ("verdict" in fields or fields.get("status") == "failed"):
            raise ConnectionError("DB 연결 끊김")
        await original(review_id, **fields)

    h.repo.update_review = update_review  # type: ignore[method-assign]
    return calls


async def _swept(h: Harness, status: str = "received") -> None:
    """review sweep 이 한 일 — reviewing 이던 행을 received 로 되돌린다 (메시지는 테스트가 다시 넣는다)."""
    h.repo.reviews[RID]["status"] = status


async def test_requested_again_continues_from_checkpoint() -> None:
    h = Harness()
    break_record_result_once(h)
    with pytest.raises(ConnectionError):  # 오프셋을 커밋하지 않고 워커가 내려간다
        await h.request(load_sample(SAMPLE_01))
    assert h.row()["status"] == "reviewing"
    assert (await _state(h))["_next"] == ("record_result",)  # 체크포인트는 record_result 앞에 남았다
    judged = h.deps.llm.calls

    await _swept(h)
    await h.handler.handle("review.requested", h.last_requested)

    assert h.row()["status"] == "waiting_ci"
    assert h.deps.llm.calls == judged  # judge 를 다시 부르지 않고 record_result 부터


async def test_requested_again_without_checkpoint_starts_over(harness: Harness) -> None:
    """claim 뒤 그래프 전에 죽었다 (DB 오류) — 체크포인트가 없으니 처음부터."""
    raw = await harness.request(load_sample(SAMPLE_01))
    await harness.graph.checkpointer.adelete_thread(RID)
    await _swept(harness)

    await harness.handler.handle("review.requested", raw)

    assert harness.row()["status"] == "waiting_ci"


async def test_requested_again_while_waiting_human_goes_back_to_needs_human() -> None:
    """사람 결정 재개 claim(needs_human→reviewing) 뒤 죽었다 — 결정은 다시 받는다."""
    h = Harness()
    sample = load_sample(SAMPLE_04)
    await _seed_baseline(h, sample)
    raw = await h.request(sample)
    assert h.row()["status"] == "needs_human"
    assert await h.repo.claim(RID, from_statuses=["needs_human"], to_status="reviewing")
    await _swept(h)

    await h.handler.handle("review.requested", raw)

    assert h.row()["status"] == "needs_human"
    assert (await _state(h))["_next"] == ("wait_human",)
    await h.human(RID, "rejected")
    assert h.row()["status"] == "rejected"
    assert h.row()["human_decided_at"] >= h.row()["judged_at"]


async def test_ci_completed_off_interrupt_rechecks_ci(harness: Harness) -> None:
    """병합 직전(guard_overlay)에 멈춘 검토를 sweep 이 waiting_ci 로 되돌리고 ci_completed — CI 확인부터 다시 한다."""
    await harness.request(load_sample(SAMPLE_01))
    harness.github.suites[HEAD] = [suite(conclusion="success")]
    config = {"configurable": {"thread_id": RID}}
    await harness.graph.aupdate_state(config, {}, as_node="check_ci")  # interrupt 를 지나간 체크포인트
    assert (await _state(harness))["_next"] == ()

    await harness.ci(RID, "unknown")

    assert harness.github.merged == [("Crystal-SBHackathon2026/sample-app", 7, HEAD)]


async def test_retry_overlay_reruns_only_commit_overlay() -> None:
    calls: list[str] = []

    async def overlay(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(state["spec_ref"]["commit"])
        if len(calls) == 1:
            raise RuntimeError("gitops push 실패")
        return {"deploy_result": {"status": "committed", "commit_sha": "d" * 40, "reason": None}}

    h = Harness(commit_overlay=overlay)
    await h.request(load_sample(SAMPLE_01))
    await _finish_ci(h)
    assert (h.row()["status"], h.row()["merge_sha"]) == ("merging", MERGE_SHA)

    msg = RetryOverlayResumed(review_id=RID, resumed_at=datetime.now(UTC))
    await h.handler.handle("review.resumed", msg.model_dump_json().encode())

    row = h.row()
    assert (row["status"], row["gitops_commit_sha"]) == ("committed", "d" * 40)
    assert calls == [HEAD, HEAD]
    assert len(h.github.merged) == 1  # 다시 병합하지 않는다

    await h.handler.handle("review.resumed", msg.model_dump_json().encode())  # 두 번 와도 건너뛴다
    assert calls == [HEAD, HEAD]


async def test_retry_overlay_ignores_reviews_not_merged(harness: Harness) -> None:
    await harness.request(load_sample(SAMPLE_01))
    msg = RetryOverlayResumed(review_id=RID, resumed_at=datetime.now(UTC))

    await harness.handler.handle("review.resumed", msg.model_dump_json().encode())

    assert harness.row()["status"] == "waiting_ci"
    assert harness.github.merged == []
