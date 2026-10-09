"""명세 없음·빈 명세·형식 오류 PR 을 어떻게 할지 정한다 — 커밋할 deploy.yaml 을 만들거나 거절 사유를 낸다.

네트워크·Git 쓰기 없음. 기록·커밋·PR 표시는 Review API 몫이다 (review_api.intake).

- missing·empty → prepare_spec 으로 생성. 확인 안 된 값(verification)이 남으면 커밋하지 않는다 — 추정값이
  자동 검토·병합·배포로 이어지지 않게. baseline 이 있으면 그 설정을 보존하므로 확인 항목이 없다.
  새 앱은 레포 분석(analyze.analyze_repository)이 근거가 분명한 값만 context 에 채운다. 그 근거(findings)는
  생성 커밋 메시지와 UNVERIFIED 사유에 그대로 쓴다.
- yaml_error·schema_error → repair.repair_intake 가 LLM 으로 형식만 고치고 코드 게이트로 값을 대조한다(repaired).
  LLM 이 없을 때 prepare_intake 로 오면 REPAIR_UNAVAILABLE.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from review_ai.intake.analyze import Finding
from review_ai.overlay.yaml_io import dump_with_header
from review_ai.preparation import GenerationContext, prepare_spec
from review_ai.secrets_pattern import MASK
from review_ai.spec.deploy_spec import Baseline

IntakeKind = Literal["missing", "empty", "yaml_error", "schema_error"]
KIND_LABELS: dict[str, str] = {"missing": "deploy.yaml 없음", "empty": "deploy.yaml 이 비었음",
                               "yaml_error": "deploy.yaml YAML 문법 오류", "schema_error": "deploy.yaml 형식 오류"}


@dataclass(frozen=True)
class IntakeOutcome:
    action: Literal["generated", "repaired", "rejected"]
    reason: str  # GENERATED · REPAIRED · REPAIR_UNAVAILABLE · REPAIR_REJECTED · UNVERIFIED · MASKED_VALUE
    message: str                             # 사람이 읽는 한 줄 — PR 상태 설명·커밋 메시지 제목
    content: str | None = None               # 커밋할 deploy.yaml 원문 (generated 일 때만)
    details: tuple[dict[str, str], ...] = ()  # generated·repaired: 값 출처, 거절: 확인 안 된 항목·게이트 위반


def prepare_intake(kind: IntakeKind, *, context: GenerationContext, baseline: Baseline | None = None,
                   findings: Sequence[Finding] = ()) -> IntakeOutcome:
    """findings 는 레포 분석 결과 — context 에 채운 값의 근거와 채우지 못한 이유."""
    if kind not in ("missing", "empty"):
        return IntakeOutcome("rejected", "REPAIR_UNAVAILABLE",
                             f"{KIND_LABELS[kind]} — 자동 복구는 아직 없다. 오류를 고쳐 다시 올려라")
    prepared = prepare_spec(None, context=context, baseline=baseline)
    if prepared.verification:
        codes = ", ".join(v.code for v in prepared.verification)
        return IntakeOutcome("rejected", "UNVERIFIED",
                             f"{KIND_LABELS[kind]} — 생성에 필요한 값을 확인하지 못했다: {codes}",
                             details=tuple(_with_reason(v.as_dict(), findings) for v in prepared.verification))
    spec = prepared.spec.model_dump(mode="json", exclude_none=True)
    content = dump_with_header(spec, "review-service 가 생성한 명세 — 값을 고쳐 커밋하면 다시 검토한다")
    if MASK in content:  # baseline 이 가린 사본이면 원문 비밀을 모른다 — 가린 문자열을 커밋하지 않는다
        return IntakeOutcome("rejected", "MASKED_VALUE",
                             f"{KIND_LABELS[kind]} — 이전 승인 명세에 가린 값이 있어 생성하지 않았다")
    source = "이전 승인 명세(baseline)" if baseline is not None else "레포 분석" if findings else "확인된 앱 설정"
    evidence = tuple(f.as_dict() for f in findings if f.resolved)
    return IntakeOutcome("generated", "GENERATED", f"{KIND_LABELS[kind]} — {source}로 deploy.yaml 생성",
                         content=content, details=evidence + tuple(r.as_dict() for r in prepared.recommendations))


def _with_reason(item: dict[str, str], findings: Sequence[Finding]) -> dict[str, str]:
    """확인 항목에 레포 분석이 그 값을 못 채운 이유를 붙인다."""
    found = next((f for f in findings if f.path == item["path"] and not f.resolved), None)
    return {**item, "message": found.reason, "source": found.source} if found else item


def commit_message(outcome: IntakeOutcome) -> str:
    """생성·복구 커밋 메시지 — 제목 + 어떤 값을 어디서 가져왔는지(복구면 무엇을 왜 바꿨는지)."""
    lines = [f"{'fix' if outcome.action == 'repaired' else 'chore'}: {outcome.message}", ""]
    lines += [f"- {d['path']}: {d['source']} — {d['reason']}" for d in outcome.details]
    return "\n".join(lines).rstrip() + "\n"
