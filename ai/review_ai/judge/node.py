"""judge 노드 — LLM 호출 → 출력 검증 → decide_verdict. decision·patch 두 필드만 쓴다.

- findings 0건: LLM 없이 pass
- low 경고뿐: LLM 없이 pass (설명은 규칙 문서 제목으로). 매 배포 경고 설명에 비용을 쓰지 않는다
- LLM 일시 오류: TransientError 를 그대로 올린다 → 파이프라인 RetryPolicy. 재시도 후에도 실패하면 파이프라인이
  judge_unavailable(state) 결과로 State 를 채우고 계속한다
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from review_ai.judge.llm import LlmClient, LlmRefused, LlmUnavailable
from review_ai.judge.prompt import PROMPT_VERSION, build_request
from review_ai.judge.schema import LlmItem, LlmReview
from review_ai.judge.validate import validate_output
from review_ai.state import Finding
from review_ai.verdict import Validation, decide_verdict

Node = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
log = logging.getLogger(__name__)
SKIP = Validation(llm_available=False, schema_ok=True, citations_ok=True, patch_scope_ok=True)


def _warning_review(findings: list[Finding]) -> LlmReview:
    items = [LlmItem(finding_id=f["finding_id"], cited_rule_ids=[f["rule_id"]], why=f"경고: {f['title']}", fix_kind="none")
             for f in findings]
    return LlmReview(items=items)


def _result(state: dict[str, Any], review: LlmReview | None, validation: Validation,
            meta: dict[str, Any] | None, patch: Any = None) -> dict[str, Any]:
    decision = decide_verdict(state.get("findings") or [], state.get("retrieved_docs") or [], review, validation,
                              rounds=state.get("rounds") or [], llm_meta=meta)
    return {"decision": decision, "patch": patch if decision["verdict"] == "fix" else None}


def judge_unavailable(state: dict[str, Any], error: str = "llm_not_configured") -> dict[str, Any]:
    """LLM 을 쓸 수 없을 때의 judge 결과. 정적 검사 결과(findings)는 State 에 그대로 남고, 원인은 llm.error 에."""
    return _result(state, None, Validation(llm_available=False), {"error": error})


def make_judge(llm: LlmClient | None) -> Node:
    async def judge(state: dict[str, Any]) -> dict[str, Any]:
        findings: list[Finding] = state.get("findings") or []
        if not findings:
            return _result(state, None, SKIP, None)
        if all(f["severity"] == "low" for f in findings):
            return _result(state, _warning_review(findings), SKIP, None)
        if llm is None:
            log.warning("review %s: LLM 클라이언트 없음 → LLM_UNAVAILABLE", state.get("review_id"))
            return judge_unavailable(state)
        docs = state.get("retrieved_docs") or []
        request = build_request(state["deploy_spec"], findings, docs, state["target_env"])
        meta: dict[str, Any] = {"model": llm.model, "prompt_version": PROMPT_VERSION, "input_hash": request.input_hash}
        try:
            response = await llm.complete(request)
        except LlmUnavailable as exc:
            log.error("review %s: LLM 사용 불가 (%s) → LLM_UNAVAILABLE", state.get("review_id"), exc)
            return judge_unavailable(state, error=f"unavailable: {exc}")
        except LlmRefused:
            log.warning("review %s: 모델 거절 → CITATION_INVALID", state.get("review_id"))
            invalid = Validation(llm_available=True, schema_ok=False, citations_ok=False, patch_scope_ok=False)
            return _result(state, None, invalid, {**meta, "refused": True})
        review, validation, patch = validate_output(response.text, findings, docs, state["deploy_spec"])
        meta = {**meta, "model": response.model, "usage": response.usage, "stop_reason": response.stop_reason}
        if not (validation.get("schema_ok") and validation.get("citations_ok") and validation.get("patch_scope_ok")):
            log.warning("review %s: LLM 출력 검증 실패 %s (stop_reason=%s)", state.get("review_id"),
                        validation, response.stop_reason)
        return _result(state, review, validation, meta, patch)

    return judge
