"""명세 없음·빈 명세·형식 오류 PR 을 어떻게 할지 정한다 — 커밋할 deploy.yaml 을 만들거나 거절 사유를 낸다.

네트워크·Git 쓰기 없음. 기록·커밋·PR 표시는 Review API 몫이다 (review_api.intake).

- missing·empty → prepare_spec 으로 생성. 확인 안 된 값(verification)이 남으면 커밋하지 않는다 — 추정값이
  자동 검토·병합·배포로 이어지지 않게. baseline 이 있으면 그 설정을 보존하므로 확인 항목이 없다.
  새 앱은 레포 분석(context 채우기)이 붙어야 확인 항목이 사라진다.
- yaml_error·schema_error → 아직 고치지 않는다(REPAIR_UNAVAILABLE). LLM 복구가 붙으면 repaired 가 된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from review_ai.overlay.yaml_io import dump_with_header
from review_ai.preparation import GenerationContext, prepare_spec
from review_ai.secrets_pattern import MASK
from review_ai.spec.deploy_spec import Baseline

IntakeKind = Literal["missing", "empty", "yaml_error", "schema_error"]
KIND_LABELS: dict[str, str] = {"missing": "deploy.yaml 없음", "empty": "deploy.yaml 이 비었음",
                               "yaml_error": "deploy.yaml YAML 문법 오류", "schema_error": "deploy.yaml 형식 오류"}


@dataclass(frozen=True)
class IntakeOutcome:
    action: Literal["generated", "rejected"]
    reason: str                              # GENERATED · REPAIR_UNAVAILABLE · UNVERIFIED · MASKED_VALUE
    message: str                             # 사람이 읽는 한 줄 — PR 상태 설명·커밋 메시지 제목
    content: str | None = None               # 커밋할 deploy.yaml 원문 (generated 일 때만)
    details: tuple[dict[str, str], ...] = ()  # generated: 권장값 출처, UNVERIFIED: 확인 안 된 항목


def prepare_intake(kind: IntakeKind, *, context: GenerationContext,
                   baseline: Baseline | None = None) -> IntakeOutcome:
    if kind not in ("missing", "empty"):
        return IntakeOutcome("rejected", "REPAIR_UNAVAILABLE",
                             f"{KIND_LABELS[kind]} — 자동 복구는 아직 없다. 오류를 고쳐 다시 올려라")
    prepared = prepare_spec(None, context=context, baseline=baseline)
    if prepared.verification:
        codes = ", ".join(v.code for v in prepared.verification)
        return IntakeOutcome("rejected", "UNVERIFIED",
                             f"{KIND_LABELS[kind]} — 생성에 필요한 값을 확인하지 못했다: {codes}",
                             details=tuple(v.as_dict() for v in prepared.verification))
    spec = prepared.spec.model_dump(mode="json", exclude_none=True)
    content = dump_with_header(spec, "review-service 가 생성한 명세 — 값을 고쳐 커밋하면 다시 검토한다")
    if MASK in content:  # baseline 이 가린 사본이면 원문 비밀을 모른다 — 가린 문자열을 커밋하지 않는다
        return IntakeOutcome("rejected", "MASKED_VALUE",
                             f"{KIND_LABELS[kind]} — 이전 승인 명세에 가린 값이 있어 생성하지 않았다")
    source = "이전 승인 명세(baseline)" if baseline is not None else "확인된 앱 설정"
    return IntakeOutcome("generated", "GENERATED", f"{KIND_LABELS[kind]} — {source}로 deploy.yaml 생성",
                         content=content, details=tuple(r.as_dict() for r in prepared.recommendations))


def commit_message(outcome: IntakeOutcome) -> str:
    """생성 커밋 메시지 — 제목 + 어떤 값을 어디서 가져왔는지."""
    lines = [f"chore: {outcome.message}", ""]
    lines += [f"- {d['path']}: {d['source']} — {d['reason']}" for d in outcome.details]
    return "\n".join(lines).rstrip() + "\n"
