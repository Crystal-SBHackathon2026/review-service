"""워커 진입점 — judge LLM 클라이언트 설정, 메인 루프와 livenessProbe 파일."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import review_worker.main as main_module
from review_worker.graph import JUDGE_MAX_RETRIES
from review_worker.main import AliveFile, consume, keep_alive_while, make_llm

WORST_CASE_SECONDS = 10 * 60  # SDK 기본값(600초·재시도 2번)이면 한 검토가 judge 에서 최대 12×600초 멈췄다


def test_judge_llm_is_bounded_and_retried_only_by_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = make_llm()._inner._client  # type: ignore[union-attr]

    assert client.max_retries == 0  # 재시도는 그래프 judge 노드(JUDGE_MAX_RETRIES) 한 곳에서만
    assert client.timeout * (JUDGE_MAX_RETRIES + 1) < WORST_CASE_SECONDS
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert make_llm() is None


# --- livenessProbe — /tmp/worker-alive (P2) -----------------------------------------------------------

class FakeConsumer:
    """getmany 를 부를 때마다 batches 를 하나씩 준다. 다 주면 stop 을 켠다."""

    def __init__(self, stop: asyncio.Event, *batches: list[tuple[str, bytes]]) -> None:
        self.stop, self.batches, self.polls, self.commits = stop, list(batches), 0, 0

    async def getmany(self, *, timeout_ms: int) -> dict[str, list[Any]]:
        self.polls += 1
        if not self.batches:
            self.stop.set()
            return {}
        return {"tp": [SimpleNamespace(topic=t, value=v) for t, v in self.batches.pop(0)]}

    async def commit(self) -> None:
        self.commits += 1


class SlowHandler:
    def __init__(self, seconds: float = 0.0) -> None:
        self.seconds, self.handled = seconds, []

    async def handle(self, topic: str, value: bytes) -> None:
        await asyncio.sleep(self.seconds)
        self.handled.append(value)


class CountingAlive(AliveFile):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.touches = 0

    def touch(self) -> None:
        self.touches += 1
        super().touch()


async def test_alive_file_touched_every_poll_even_without_messages(tmp_path: Path) -> None:
    stop = asyncio.Event()
    alive = CountingAlive(tmp_path / "worker-alive")
    consumer = FakeConsumer(stop, [], [], [])  # 메시지 없는 poll 세 번

    await consume(consumer, SlowHandler(), stop, alive, handle_limit_seconds=900)

    assert alive.path.exists()
    assert alive.touches == consumer.polls == 4


async def test_alive_file_touched_while_handling_long_message(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """judge 재시도로 처리가 길어도 probe(2분) 에 죽지 않게 처리 중에도 갱신한다."""
    monkeypatch.setattr(main_module, "ALIVE_TOUCH_SECONDS", 0.01)
    stop = asyncio.Event()
    alive = CountingAlive(tmp_path / "worker-alive")
    handler = SlowHandler(seconds=0.1)
    consumer = FakeConsumer(stop, [("review.requested", b"m1")])

    await consume(consumer, handler, stop, alive, handle_limit_seconds=900)

    assert handler.handled == [b"m1"] and consumer.commits == 1
    assert alive.touches >= 5  # poll 2번 + 처리 중 여러 번


async def test_alive_file_stops_after_handle_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_poll_interval 을 넘게 한 메시지에 묶이면 갱신을 멈춘다 — probe 가 재시작한다."""
    monkeypatch.setattr(main_module, "ALIVE_TOUCH_SECONDS", 0.01)
    alive = CountingAlive(tmp_path / "worker-alive")

    await keep_alive_while(alive, asyncio.sleep(0.2), limit_seconds=0.03)

    assert 1 <= alive.touches <= 4


async def test_handler_exception_still_stops_the_loop(tmp_path: Path) -> None:
    """처리 밖 예외(DB 연결)는 그대로 올라가 오프셋을 커밋하지 않고 프로세스를 내린다."""
    class Broken:
        async def handle(self, topic: str, value: bytes) -> None:
            raise ConnectionError("DB 연결 끊김")

    stop = asyncio.Event()
    consumer = FakeConsumer(stop, [("review.requested", b"m1")])

    with pytest.raises(ConnectionError):
        await consume(consumer, Broken(), stop, AliveFile(tmp_path / "a"), handle_limit_seconds=900)
    assert consumer.commits == 0


async def test_analysis_routing_preserves_review_consumption_and_alive(tmp_path):
    from review_common.deployment import TOPIC
    from review_worker.main import RoutedHandler
    class Analysis:
        def __init__(self): self.values = []
        async def handle(self, value): self.values.append(value)
    stop = asyncio.Event()
    consumer = FakeConsumer(stop, [("review.requested", b"review"), (TOPIC, b"evidence")])
    review, analysis = SlowHandler(), Analysis()
    alive = CountingAlive(tmp_path / "alive")
    await consume(consumer, RoutedHandler(review, analysis), stop, alive, handle_limit_seconds=900)
    assert review.handled == [b"review"] and analysis.values == [b"evidence"]
    assert consumer.commits == 2 and alive.touches == consumer.polls
