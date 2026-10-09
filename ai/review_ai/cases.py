"""끝난 검토 → 판단 사례(업무 DB review_cases). 다음 검토가 같은 규칙에 걸리면 근거 문서로 붙는다.

워커가 종료 지점(사람 거절·AI 수정 커밋·사람 승인 후 gitops 커밋)에서, API 가 Argo CD Degraded 에서 만든다.
내용은 마스킹된 rounds·findings 에서만 만든다 — finding 의 evidence(값)는 넣지 않고 규칙·경로·제목만 쓰고,
적용한 ops 는 비밀이 들어갈 수 있는 경로(env·secrets·baseline)와 비밀처럼 보이는 값을 뺀다.

권장값(recommendations)에는 쓰지 않는다. 규칙·baseline 으로 정할 수 없는 값(빌드 플랫폼·시크릿 출처)은
다른 검토에서 사람이 고른 값을 옮겨 와도 이 배포에 대한 관측이 아니다. 사례는 judge 의 근거로만 쓴다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from review_ai.patching import parse_pointer
from review_ai.secrets_pattern import has_secret_value
from review_ai.state import Finding
from review_ai.verdict import applied_ops

OUTCOME_TEXT = {
    "auto_fixed": "AI 가 자동 수정했고 재검사를 통과했다",
    "human_approved": "사람이 명세를 고치지 않고 승인했다",
    "human_edited": "사람이 값을 직접 고쳐 승인했다",
    "recommended": "사람이 권장값으로 승인했다",
    "rejected": "사람이 거절했다",
    "deploy_degraded": "승인·병합됐지만 배포 뒤 Argo CD 가 Degraded 로 보고했다",
}
PRIVATE_PATHS = ("/runtime/env", "/secrets", "/baseline")
MAX_SUMMARY_VALUE = 80


def case_outcome(human: Mapping[str, Any] | None) -> str:
    """사람 응답으로 종료 방식을 정한다. 응답이 없으면 AI 수정으로 끝난 것이다."""
    if not human:
        return "auto_fixed"
    if human["decision"] == "rejected":
        return "rejected"
    defaulted = human.get("defaulted_ops") or []
    if any(op not in defaulted for op in human.get("edited_ops") or []):
        return "human_edited"
    return "recommended" if defaulted else "human_approved"


def _private(path: str) -> bool:
    if path == "":  # 문서 전체 교체 — env·secrets 를 품는다
        return True
    tokens = parse_pointer(path)
    for private in PRIVATE_PATHS:
        expected = parse_pointer(private)
        if tokens[:len(expected)] == expected or expected[:len(tokens)] == tokens:  # 아래이거나 그 위(통째 교체)
            return True
    return False


def case_ops(ops: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """사례에 남길 ops — 비밀이 들어갈 수 있는 경로와 비밀처럼 보이는 값을 뺀다."""
    kept = []
    for op in ops:
        if _private(op["path"]) or has_secret_value(json.dumps(op.get("value"), ensure_ascii=False)):
            continue
        kept.append(dict(op))
    return kept


def _decided_findings(rounds: Sequence[Mapping[str, Any]], findings: Sequence[Finding]) -> list[Finding]:
    """판단이 필요했던 finding — 회차 기록과 마지막 findings 에서 low 경고를 빼고 (규칙, 경로)로 한 번씩."""
    seen: set[tuple[str, str]] = set()
    out = []
    for finding in [f for r in rounds for f in r.get("findings") or []] + list(findings):
        key = (finding["rule_id"], finding["location"].get("spec_path", ""))
        if finding["severity"] != "low" and key not in seen:
            seen.add(key)
            out.append(finding)
    return out


def _value(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= MAX_SUMMARY_VALUE else text[:MAX_SUMMARY_VALUE] + "…"


def build_case(*, review_id: str, app: str, target_env: str, outcome: str, rounds: Sequence[Mapping[str, Any]],
               findings: Sequence[Finding], reasons: Sequence[str], ops: Sequence[Mapping[str, Any]]
               ) -> dict[str, Any] | None:
    """review_cases 한 행. 판단이 필요했던 finding 이 없으면(경고만 있던 통과) None — 남길 판단이 없다."""
    if outcome not in OUTCOME_TEXT:
        raise ValueError(f"모르는 사례 종료 방식: {outcome}")
    decided = _decided_findings(rounds, findings)
    if not decided:
        return None
    kept = [] if outcome == "rejected" else case_ops(ops)
    lines = [f"지난 검토 {review_id} ({app}, {target_env}): {OUTCOME_TEXT[outcome]}."]
    lines += [f"- {f['rule_id']} {f['title']} ({f['location'].get('spec_path', '')})" for f in decided]
    if reasons:
        lines.append(f"사람 확인 사유: {', '.join(sorted(set(reasons)))}")
    if kept:
        lines.append("적용한 값: " + ", ".join(
            f"{op['path']} 삭제" if op["op"] == "remove" else f"{op['path']} = {_value(op.get('value'))}"
            for op in kept))
    return {
        "case_id": f"{review_id}.{outcome}", "review_id": review_id, "app": app, "target_env": target_env,
        "rule_ids": sorted({f["rule_id"] for f in decided}), "outcome": outcome, "summary": "\n".join(lines),
        "ops": kept,
    }


def _reasons(rounds: Sequence[Mapping[str, Any]], decision: Mapping[str, Any] | None) -> list[str]:
    return [reason for r in rounds for reason in r.get("reasons") or []] + list((decision or {}).get("reasons") or [])


def case_from_state(state: Mapping[str, Any], *, human: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """워커 종료 지점의 State → 사례. human 을 주면 State 의 human_decision 대신 쓴다 (거절은 State 에 넣기 전에 끝난다)."""
    rounds = state.get("rounds") or []
    human = human if human is not None else state.get("human_decision")
    return build_case(review_id=state["review_id"], app=state["deploy_spec"]["metadata"]["name"],
                      target_env=state["target_env"], outcome=case_outcome(human), rounds=rounds,
                      findings=state.get("findings") or [], reasons=_reasons(rounds, state.get("decision")),
                      ops=applied_ops(state))


def case_from_review(row: Mapping[str, Any], outcome: str) -> dict[str, Any] | None:
    """업무 DB reviews 행 → 사례 (Argo CD Degraded 처럼 워커 밖에서 끝을 아는 경우)."""
    rounds = row.get("rounds") or []
    return build_case(review_id=row["review_id"], app=row["app"], target_env=row["target_env"], outcome=outcome,
                      rounds=rounds, findings=row.get("findings") or [], reasons=_reasons(rounds, row.get("decision")),
                      ops=applied_ops({"rounds": rounds}))
