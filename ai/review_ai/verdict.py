"""decide_verdict() — verdict 는 LLM 이 아니라 이 결정적 함수가 정한다. 라우터는 결과를 그대로 따른다.

판정 순서 (먼저 걸리는 것이 이긴다)
1. findings 0건 → pass (검색·LLM 없음)
2. 사람 확인 조건 하나라도 → needs_human + 사유 코드
3. 남은 finding 이 전부 검증된 패치 대상 → fix
4. 남은 것이 low 경고뿐 → pass

spec_unverified(intake 가 baseline 없이 만든 명세)면 findings 가 없어도 needs_human(GENERATED_SPEC_UNVERIFIED) 이다.
레포 분석만으로는 공개 범위·env·replicas 를 모른다 — 빈 network 가 그대로 통과해 ingress 가 지워진 장애(10/09)가 있었다.
다른 사유가 있으면 같이 낸다. fix 도 하지 않는다 — 명세 자체를 사람이 확인해야 하므로 AI 가 먼저 고치지 않는다.

3 에서 자동 수정이 막혀 있으면(autofix_allowed=False) fix 대신 needs_human(LOOP_EXHAUSTED) 이다. 둘 다 "AI 가 또 고치려는" 경우다.
- 봇 커밋 재검토: 워커가 applied_ops 를 커밋한 SHA 를 다시 검토했는데 또 fix — 다시 커밋하면 무한 루프
- 사람이 고친 뒤 재검사: 사람이 손댄 명세를 AI 가 이어서 고치지 않는다
사유 코드를 새로 만들지 않고 LOOP_EXHAUSTED 를 쓴다 — 업무 DB 의 사유 코드 8개를 그대로 둔다.

low finding 은 사람 확인 조건에서 뺀다 (결정 #6). 안 빼면 HTTP 전용 sample-app(NET-001)이 매번 needs_human 이 된다.
단, LLM 출력이 틀렸으면(CITATION_INVALID) low 만 있어도 needs_human — 틀린 설명을 그대로 내보내지 않는다.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from review_ai.judge.schema import LlmReview
from review_ai.state import REASON_CODES, Decision, Doc, Finding, PatchOp

MAX_PATCH_ROUNDS = 2
LOW_SCORE_THRESHOLD = 0.5  # dense cosine 기준. ruleId 정확 매칭에는 쓰지 않는다
EXCLUDE_LOW_FROM_HUMAN = True


class Validation(TypedDict, total=False):
    llm_available: bool
    schema_ok: bool
    citations_ok: bool
    patch_scope_ok: bool


def _counted(findings: Sequence[Finding]) -> list[Finding]:
    if not EXCLUDE_LOW_FROM_HUMAN:
        return list(findings)
    return [f for f in findings if f["severity"] != "low"]


def _low_score(findings: Sequence[Finding], docs: Sequence[Doc]) -> bool:
    # 지난 검토 사례(doc_type case)는 규칙 문서를 대신하지 않는다 — 문서 없는 규칙이 사례만으로 근거를 갖추면 안 된다
    exact = {d["rule_id"] for d in docs if d["match"] == "exact_rule" and d["doc_type"] != "case"}
    best_semantic = max((d["score"] for d in docs if d["match"] == "semantic"), default=0.0)
    return any(f["rule_id"] not in exact for f in findings) and best_semantic < LOW_SCORE_THRESHOLD


def _loop_exhausted(findings: Sequence[Finding], rounds: Sequence[dict[str, Any]]) -> bool:
    if not rounds:
        return False
    if len(rounds) >= MAX_PATCH_ROUNDS:
        return True
    current, previous = {f["finding_id"] for f in findings}, set(rounds[-1]["finding_ids"])
    return not current < previous  # 줄지 않았거나 새 finding 이 생김


def _patch_covers(findings: Sequence[Finding], llm_out: LlmReview | None) -> bool:
    if llm_out is None or llm_out.patch is None:
        return False
    return {f["finding_id"] for f in findings} <= set(llm_out.patch.target_finding_ids)


def _human_reasons(
    findings: Sequence[Finding],
    docs: Sequence[Doc],
    llm_out: LlmReview | None,
    validation: Validation,
    rounds: Sequence[dict[str, Any]],
) -> set[str]:
    counted = _counted(findings)
    available = validation.get("llm_available", False)
    reasons: set[str] = set()
    if available and not (validation.get("schema_ok") and validation.get("citations_ok")):
        reasons.add("CITATION_INVALID")
    if not counted:
        return reasons
    if not available:
        reasons.add("LLM_UNAVAILABLE")
    if _low_score(counted, docs):
        reasons.add("LOW_SCORE")
    for f in counted:
        if f["autofix"] == "forbidden":
            reasons.add("IRREVERSIBLE" if f["irreversible"] else "AUTOFIX_FORBIDDEN")
    if llm_out is not None and llm_out.patch is not None and not validation.get("patch_scope_ok"):
        reasons.add("PATCH_OUT_OF_SCOPE")
    if _loop_exhausted(findings, rounds):
        reasons.add("LOOP_EXHAUSTED")
    if not reasons and not _patch_covers(counted, llm_out):
        reasons.add("PATCH_MISSING")
    return reasons


def decide_verdict(
    findings: Sequence[Finding],
    docs: Sequence[Doc],
    llm_out: LlmReview | None,
    validation: Validation,
    *,
    rounds: Sequence[dict[str, Any]] = (),
    llm_meta: dict[str, Any] | None = None,
    autofix_allowed: bool = True,
    spec_unverified: bool = False,
) -> Decision:
    items = [item.model_dump() for item in llm_out.items] if llm_out else []
    extra = list(llm_out.extra_opinions) if llm_out else []
    base = Decision(verdict="pass", reasons=[], items=items, extra_opinions=extra,
                    validation=dict(validation), llm=llm_meta)  # type: ignore[typeddict-item]
    if not findings and not spec_unverified:
        return base
    reasons = _human_reasons(findings, docs, llm_out, validation, rounds) if findings else set()
    if spec_unverified:
        reasons.add("GENERATED_SPEC_UNVERIFIED")
    if reasons:
        ordered = [code for code in REASON_CODES if code in reasons]
        return {**base, "verdict": "needs_human", "reasons": ordered}
    if not _counted(findings):
        return base
    if not autofix_allowed:
        return {**base, "verdict": "needs_human", "reasons": ["LOOP_EXHAUSTED"]}
    return {**base, "verdict": "fix"}


def round_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """apply_patch 직전에 rounds 에 추가할 회차 기록. 덮어쓰지 않고 쌓는다.

    고쳐서 통과하면 최종 findings·decision·retrieved_docs 는 재검사 결과(비어 있음)로 바뀌므로,
    무엇을 왜 고쳤는지(finding·AI 설명·근거 문서 ID)는 이 기록에만 남는다. 결과 화면·업무 DB 가 여기서 읽는다.
    """
    decision = state["decision"]
    snapshot = {
        "round": state.get("retry_count", 0),
        "finding_ids": [f["finding_id"] for f in state["findings"]],
        "findings": copy.deepcopy(state["findings"]),
        "verdict": decision["verdict"],
        "reasons": list(decision["reasons"]),
        "items": copy.deepcopy(decision["items"]),
        "doc_ids": [d["chunk_id"] for d in state.get("retrieved_docs") or []],
        "patch": state.get("patch"),
    }
    if decision.get("recommendations"):
        snapshot["recommendations"] = copy.deepcopy(decision["recommendations"])
    return snapshot


def applied_ops(state: Mapping[str, Any]) -> list[PatchOp]:
    """원본 deploy_spec 에 적용할 수정 지시서 — 회차별 패치를 순서대로 이어 붙인다. 커밋 단계가 이걸 쓴다.

    apply_patch 는 patch 를 rounds 로 옮기고 비우므로, fix 루프를 돈 최종 State 는 verdict=pass·patch=None 이고
    실제 ops 는 rounds[*].patch.ops 에만 있다. 마지막 judge 가 fix 로 끝났으면(아직 적용 전) 현재 patch.ops 를 덧붙인다.
    입력 State 는 바꾸지 않는다 (ops 는 복사본).
    """
    patches = [r.get("patch") for r in state.get("rounds") or []]
    if (state.get("decision") or {}).get("verdict") == "fix":
        patches.append(state.get("patch"))
    return [copy.deepcopy(op) for patch in patches if patch for op in patch["ops"]]
