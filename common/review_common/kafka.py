"""Kafka 발행 — Review API·워커가 같은 publisher 를 쓴다.

send_and_wait 는 브로커가 응답할 때까지 앱 쪽 상한 없이 기다린다. MSK 가 느리거나 끊기면 GitHub 웹훅 응답(10초 제한)과
워커 루프가 같이 묶인다 — KAFKA_SEND_TIMEOUT(기본 5초)을 넘으면 TransientError 로 올린다.
API 는 발행 실패와 같은 503 으로, 워커는 그래프 실패로 처리하고 review sweep 이 멈춘 검토를 다시 발행한다.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Protocol

from prometheus_client import Counter

from review_ai.errors import TransientError

DEFAULT_SEND_TIMEOUT = 5.0

PUBLISH = Counter("review_kafka_publish", "Kafka 발행 결과 (API·워커 각자)", ["topic", "result"])  # ok·timeout·error


def send_timeout() -> float:
    return float(os.environ.get("KAFKA_SEND_TIMEOUT") or DEFAULT_SEND_TIMEOUT)


class Producer(Protocol):
    async def send_and_wait(self, topic: str, value: bytes | None = None, key: bytes | None = None,
                            **kwargs: Any) -> Any: ...


class KafkaPublisher:
    def __init__(self, producer: Producer, *, timeout: float | None = None) -> None:
        self._producer = producer
        self.timeout = timeout if timeout is not None else send_timeout()

    async def send(self, topic: str, key: str, value: bytes) -> None:
        try:
            await asyncio.wait_for(self._producer.send_and_wait(topic, value=value, key=key.encode()), self.timeout)
        except TimeoutError as exc:
            PUBLISH.labels(topic, "timeout").inc()
            raise TransientError(f"Kafka {topic} 발행이 {self.timeout:g}초 안에 끝나지 않았다") from exc
        except Exception:
            PUBLISH.labels(topic, "error").inc()
            raise
        PUBLISH.labels(topic, "ok").inc()
