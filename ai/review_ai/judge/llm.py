"""LLM 클라이언트. judge 노드는 LlmClient 프로토콜만 알고, 실제 Claude·가짜 구현은 팩토리로 주입한다."""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

from review_ai.errors import TransientError
from review_ai.judge.prompt import JudgeRequest
from review_ai.judge.schema import LlmReview

DEFAULT_MODEL = os.environ.get("REVIEW_LLM_MODEL", "claude-opus-5-5")
MAX_TOKENS = 8000


class LlmUnavailable(RuntimeError):
    """키 없음·권한 없음 — 재시도해도 소용없다. judge 가 LLM_UNAVAILABLE 로 끝낸다."""


class LlmRefused(RuntimeError):
    """모델 거절 — 도메인 결과. 출력이 없으므로 스키마 위반(CITATION_INVALID)으로 처리한다."""


@dataclass(frozen=True)
class LlmResponse:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None  # max_tokens 면 출력이 잘려 스키마 검증에서 떨어진다


class LlmClient(Protocol):
    model: str

    async def complete(self, request: JudgeRequest) -> LlmResponse: ...


class ClaudeLLM:
    """Claude API. structured outputs 로 LlmReview 스키마를 강제하고, 시스템 프롬프트는 캐시한다."""

    def __init__(self, client: Any = None, model: str = DEFAULT_MODEL) -> None:
        import anthropic

        if client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise LlmUnavailable("ANTHROPIC_API_KEY 가 없다")
            client = anthropic.AsyncAnthropic()
        self._anthropic = anthropic
        self._client = client
        self._schema = anthropic.transform_schema(LlmReview.model_json_schema())
        self.model = model

    async def complete(self, request: JudgeRequest) -> LlmResponse:
        a = self._anthropic
        try:
            msg = await self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": request.user}],
                output_config={"format": {"type": "json_schema", "schema": self._schema}},
            )
        except (a.AuthenticationError, a.PermissionDeniedError) as exc:
            raise LlmUnavailable(str(exc)) from exc
        except (a.APITimeoutError, a.APIConnectionError, a.RateLimitError, a.InternalServerError) as exc:
            raise TransientError(f"Claude API 일시 오류: {type(exc).__name__}") from exc
        except a.APIStatusError as exc:
            if exc.status_code in (429, 529) or exc.status_code >= 500:
                raise TransientError(f"Claude API {exc.status_code}") from exc
            raise
        if msg.stop_reason == "refusal":
            raise LlmRefused("모델이 응답을 거절했다")
        text = "".join(block.text for block in msg.content if block.type == "text")
        return LlmResponse(text=text, model=msg.model, usage=msg.usage.model_dump(exclude_none=True),
                           stop_reason=msg.stop_reason)


class CachedLLM:
    """input_hash 기준 캐시 — Kafka 재전송·재시도로 같은 노드가 두 번 돌아도 두 번 과금되지 않게.

    같은 입력이 동시에 들어오면 첫 호출 하나만 API 로 보내고 나머지는 그 결과를 기다린다.
    기본은 프로세스 메모리 LRU. 파이프라인은 업무 DB 캐시로 바꿀 수 있다.
    """

    def __init__(self, inner: LlmClient, max_entries: int = 1024) -> None:
        self._inner = inner
        self._max = max_entries
        self._done: OrderedDict[str, LlmResponse] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[LlmResponse]] = {}
        self.model = inner.model

    async def complete(self, request: JudgeRequest) -> LlmResponse:
        key = request.input_hash
        if key in self._done:
            self._done.move_to_end(key)
            return self._done[key]
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        future: asyncio.Future[LlmResponse] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            response = await self._inner.complete(request)
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # 기다리는 쪽이 없어도 '처리 안 된 예외' 경고가 나지 않게
            raise
        finally:
            self._inflight.pop(key, None)
        future.set_result(response)
        self._done[key] = response
        if len(self._done) > self._max:
            self._done.popitem(last=False)
        return response
