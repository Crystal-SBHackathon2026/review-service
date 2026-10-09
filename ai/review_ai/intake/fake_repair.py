"""API 키 없이 형식 오류 복구(repair_intake)를 돌리기 위한 가짜 LLM — 평가셋용.

가짜는 정답 명세(answer)를 받아 만든다. LLM 은 가린 원문만 보므로 정답도 mask_spec 으로 가려서 낸다
(게이트가 같은 경로의 가린 값을 원문 비밀로 되돌리는지 같이 본다).
- oracle: 형식만 고친 정답을 낸다
- invents_value: 원문에 없는 env 값을 하나 지어 넣는다 — 게이트가 INVENTED_VALUE 로 버려야 한다
- changes_value: 원문 포트를 바꾼다 — 게이트가 VALUE_CONFLICT 로 버려야 한다
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

from review_ai.judge.fake_llm import ScriptedLLM
from review_ai.masking import mask_spec

FakeFactory = Callable[[dict[str, Any]], ScriptedLLM]


def _scripted(answer: dict[str, Any], edit: Callable[[dict[str, Any]], None], model: str) -> ScriptedLLM:
    spec = mask_spec(copy.deepcopy(answer))
    edit(spec)
    out = {"spec_json": json.dumps(spec, ensure_ascii=False), "changes": [{"path": "", "why": "형식 수정"}]}
    return ScriptedLLM(lambda _request: out, model)


def _invent(spec: dict[str, Any]) -> None:
    spec["runtime"].setdefault("env", {})["FEATURE_FLAG"] = "on"


def _change_port(spec: dict[str, Any]) -> None:
    spec["runtime"]["port"] = spec["runtime"]["port"] + 1


REPAIR_FAKES: dict[str, FakeFactory] = {
    "oracle": lambda answer: _scripted(answer, lambda _: None, "fake-repair-oracle"),
    "invents_value": lambda answer: _scripted(answer, _invent, "fake-repair-invents"),
    "changes_value": lambda answer: _scripted(answer, _change_port, "fake-repair-changes"),
}
