"""워커 지표 — 노드 처리 시간·예외, 메시지, 판정·최종 상태, LLM, consumer lag, 생존 시각. 카운터는 증가분으로 본다."""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import REGISTRY

from review_ai.errors import TransientError
from review_ai.judge.llm import LlmResponse
from review_worker import metrics
from review_worker.main import AliveFile
from tests.conftest import Harness, load_sample
from tests.test_graph import RID, SAMPLE_01, SAMPLE_04, _seed_baseline


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_node_duration_recorded() -> None:
    before = value("review_worker_node_duration_seconds_count", node="static_check")

    await Harness().request(load_sample(SAMPLE_01))

    assert value("review_worker_node_duration_seconds_count", node="static_check") == before + 1
    assert value("review_worker_node_duration_seconds_count", node="judge") >= 1


async def test_node_exception_counts_error() -> None:
    async def boom(state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("x")

    before = value("review_worker_node_errors_total", node="t_boom")
    with pytest.raises(RuntimeError):
        await metrics.timed_node("t_boom", boom)({})
    assert value("review_worker_node_errors_total", node="t_boom") == before + 1
    assert value("review_worker_node_duration_seconds_count", node="t_boom") == 0


async def test_interrupt_is_not_an_error() -> None:
    """사람 대기(interrupt)는 예외로 올라오지만 오류가 아니다."""
    h = Harness()
    sample = load_sample(SAMPLE_04)
    await _seed_baseline(h, sample)
    before = value("review_worker_node_errors_total", node="wait_human")

    await h.request(sample)

    assert h.row()["status"] == "needs_human"
    assert value("review_worker_node_errors_total", node="wait_human") == before


async def test_verdict_reasons_and_finished() -> None:
    h = Harness()
    sample = load_sample(SAMPLE_04)
    await _seed_baseline(h, sample)
    app = sample["metadata"]["name"]
    before = (value("review_worker_verdicts_total", app=app, env="aws", verdict="needs_human"),
              value("review_worker_needs_human_reasons_total", reason="IRREVERSIBLE"),
              value("review_worker_finished_total", app=app, env="aws", status="rejected"))

    await h.request(sample)
    await h.human(RID, "rejected")

    assert (value("review_worker_verdicts_total", app=app, env="aws", verdict="needs_human"),
            value("review_worker_needs_human_reasons_total", reason="IRREVERSIBLE"),
            value("review_worker_finished_total", app=app, env="aws", status="rejected")) == (
        before[0] + 1, before[1] + 1, before[2] + 1)


async def test_messages_processed_skipped_invalid() -> None:
    h = Harness()
    labels = {"topic": "review.requested", "kind": "requested"}
    before = {r: value("review_worker_messages_total", **labels, result=r) for r in ("processed", "skipped", "invalid")}

    raw = await h.request(load_sample(SAMPLE_01))
    await h.handler.handle("review.requested", raw)  # 재전송 — claim 실패
    await h.handler.handle("review.requested", b"{}")

    after = {r: value("review_worker_messages_total", **labels, result=r) for r in before}
    assert {r: after[r] - before[r] for r in after} == {"processed": 1, "skipped": 1, "invalid": 1}


class FakeLLM:
    model = "fake"

    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc

    async def complete(self, request: Any) -> LlmResponse:
        if self.exc:
            raise self.exc
        return LlmResponse(text="{}", model="fake", usage={"input_tokens": 120, "output_tokens": 30})


async def test_metered_llm_ok_and_tokens() -> None:
    before = (value("review_worker_llm_calls_total", purpose="t1", result="ok"),
              value("review_worker_llm_tokens_total", purpose="t1", type="input"),
              value("review_worker_llm_tokens_total", purpose="t1", type="output"))

    await metrics.MeteredLLM(FakeLLM(), purpose="t1").complete(object())

    assert (value("review_worker_llm_calls_total", purpose="t1", result="ok"),
            value("review_worker_llm_tokens_total", purpose="t1", type="input"),
            value("review_worker_llm_tokens_total", purpose="t1", type="output")) == (
        before[0] + 1, before[1] + 120, before[2] + 30)
    assert value("review_worker_llm_duration_seconds_count", purpose="t1") >= 1


async def test_metered_llm_timeout_and_error() -> None:
    class APITimeoutError(Exception):
        pass

    timeout = TransientError("Claude API 일시 오류: APITimeoutError")
    timeout.__cause__ = APITimeoutError()
    with pytest.raises(TransientError):
        await metrics.MeteredLLM(FakeLLM(timeout), purpose="t2").complete(object())
    with pytest.raises(ValueError):
        await metrics.MeteredLLM(FakeLLM(ValueError("bad json")), purpose="t2").complete(object())

    assert value("review_worker_llm_calls_total", purpose="t2", result="timeout") == 1
    assert value("review_worker_llm_calls_total", purpose="t2", result="error") == 1


def test_metered_llm_keeps_inner_attributes() -> None:
    inner = FakeLLM()
    inner._client = "c"  # type: ignore[attr-defined]
    wrapped = metrics.MeteredLLM(inner, purpose="judge")
    assert (wrapped.model, wrapped._client) == ("fake", "c")


TP = namedtuple("TP", "topic partition")


class FakeConsumer:
    def __init__(self, assigned: list[TP], end: dict[TP, int], committed: dict[TP, int | None],
                 begin: dict[TP, int] | None = None) -> None:
        self.assigned, self.end, self.commits, self.begin = assigned, end, committed, begin or {}

    def assignment(self) -> set[TP]:
        return set(self.assigned)

    async def end_offsets(self, partitions: list[TP]) -> dict[TP, int]:
        return {tp: self.end[tp] for tp in partitions}

    async def beginning_offsets(self, partitions: list[TP]) -> dict[TP, int]:
        return {tp: self.begin.get(tp, 0) for tp in partitions}

    async def committed(self, tp: TP) -> int | None:
        return self.commits[tp]


async def test_consumer_lag() -> None:
    a, b, c = TP("review.requested", 0), TP("review.requested", 1), TP("review.resumed", 2)
    consumer = FakeConsumer([a, b, c], end={a: 10, b: 5, c: 7}, committed={a: 7, b: 5, c: None}, begin={c: 4})

    lags = await metrics.update_lag(consumer)

    assert lags == {("review.requested", 0): 3, ("review.requested", 1): 0, ("review.resumed", 2): 3}
    assert value("review_worker_consumer_lag", topic="review.requested", partition="0") == 3
    assert value("review_worker_consumer_lag", topic="review.resumed", partition="2") == 3

    consumer.assigned = [a]  # 재할당 — 빠진 파티션은 지운다
    await metrics.update_lag(consumer)
    assert REGISTRY.get_sample_value("review_worker_consumer_lag",
                                     {"topic": "review.requested", "partition": "1"}) is None


def test_heartbeat_timestamp(tmp_path: Path) -> None:
    AliveFile(tmp_path / "alive").touch()
    import time

    assert time.time() - value("review_worker_last_heartbeat_timestamp") < 5
