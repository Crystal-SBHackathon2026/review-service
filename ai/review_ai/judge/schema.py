"""LLM 출력 스키마 — structured outputs 로 API 가 강제하고, 받은 뒤 여기서 다시 검증한다.

patch op 의 value 는 JSON 문자열(value_json)로 받는다. 임의 타입(Any)은 structured outputs 스키마로
표현하기 어렵고, 문자열로 받아 우리가 파싱하면 타입 검증을 한 곳에서 할 수 있다.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from review_ai.state import PatchOp

MAX_WHY_CHARS = 1200
MAX_OPINIONS = 5
MAX_OPINION_CHARS = 500
MAX_OPS = 10


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LlmItem(_Strict):
    finding_id: str
    cited_rule_ids: list[str] = Field(description="근거로 쓴 규칙 ID. 제공된 문서에 있는 것만")
    why: str = Field(max_length=MAX_WHY_CHARS, description="위험 설명 (한국어, 비밀 값 금지)")
    fix_kind: Literal["config", "env", "code", "none"]


class LlmPatchOp(_Strict):
    op: Literal["replace", "add", "remove"]
    path: str = Field(description="deploy_spec 기준 JSON Pointer (예: /runtime/replicas)")
    value_json: str | None = Field(default=None, description="op 가 remove 가 아니면 값의 JSON 표현")

    def to_patch_op(self) -> PatchOp:
        if self.op == "remove":
            return PatchOp(op="remove", path=self.path)
        value: Any = json.loads(self.value_json) if self.value_json is not None else None
        return PatchOp(op=self.op, path=self.path, value=value)


class LlmPatch(_Strict):
    ops: list[LlmPatchOp] = Field(min_length=1, max_length=MAX_OPS)
    target_finding_ids: list[str] = Field(min_length=1)


class LlmReview(_Strict):
    items: list[LlmItem]
    patch: LlmPatch | None = None
    extra_opinions: list[Annotated[str, Field(max_length=MAX_OPINION_CHARS)]] = Field(
        default_factory=list, max_length=MAX_OPINIONS, description="findings 밖 의견. 판정에 쓰지 않음"
    )
