"""앱 코드 변환 — 대상 환경에서 그대로 못 도는 앱을 고치는 코드 패치 (설계 '앱 분석/변환' ④·⑤).

흐름: plan_transform(레포 분석·능력표) → Claude structured output(TransformOutput) → 코드 게이트 → 커밋할 파일들.
무엇을 고칠지는 코드가 정하고(plan), LLM 은 그 항목만 코드로 옮긴다. 게이트를 하나라도 어기면 TRANSFORM_REJECTED.
네트워크·Git 쓰기·코드 실행 없음 — 커밋과 package-lock.json 재생성은 Review API 몫이다.

변환이 끝나면 생성 명세에 들어갈 값(DB·DATABASE_URL 시크릿·마이그레이션 명령)을 함께 돌려준다 → apply_to_context.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from review_ai.catalog import TargetCaps
from review_ai.intake.analyze import Finding, _source_files
from review_ai.judge.llm import LlmClient, LlmRefused, LlmUnavailable
from review_ai.preparation import GenerationContext
from review_ai.secrets_pattern import redact
from review_ai.spec.deploy_spec import Requirements, Storage
from review_ai.transform.gate import Issue, check_patch
from review_ai.transform.plan import TransformPlan, plan_transform
from review_ai.transform.prompt import TransformOutput, build_request

__all__ = ["TRANSFORM_MAX_TOKENS", "TransformOutcome", "apply_to_context", "plan_transform", "transform_repository"]

TRANSFORM_MAX_TOKENS = 16_000  # 파일 전체를 다시 쓰므로 판단(8k)보다 크게


@dataclass(frozen=True)
class TransformOutcome:
    action: Literal["patched", "rejected", "nothing"]
    reason: str  # PATCHED · NOTHING_TO_DO · TRANSFORM_UNAVAILABLE · TRANSFORM_REJECTED
    message: str
    plan: TransformPlan
    files: dict[str, str | None]                 # 경로 → 새 내용 (None = 삭제). patched 일 때만
    details: tuple[dict[str, str], ...] = ()     # patched: 파일별 이유, rejected: 게이트 위반


def _rejected(plan: TransformPlan, reason: str, message: str, issues: Sequence[Issue] = ()) -> TransformOutcome:
    return TransformOutcome("rejected", reason, message, plan, {}, tuple(i.as_dict() for i in issues))


def _summary(plan: TransformPlan) -> str:
    return " · ".join(i.reason for i in plan.items)


async def transform_repository(app: str, caps: TargetCaps, tree: Sequence[str], files: Mapping[str, str],
                               llm: LlmClient | None) -> TransformOutcome:
    """files 는 레포 분석이 읽은 파일. LLM 일시 오류(TransientError)는 올린다 — 호출한 쪽이 다시 처리한다."""
    plan = plan_transform(app, caps, tree, files)
    if not plan.items:
        return TransformOutcome("nothing", "NOTHING_TO_DO", "코드 패치가 필요 없다", plan, {})
    if llm is None:
        return _rejected(plan, "TRANSFORM_UNAVAILABLE", f"{_summary(plan)} — 코드 패치(LLM)가 설정되지 않았다")
    sources = {p: files[p] for p in _source_files(tree) if p in files}
    originals = {**sources, **({"package.json": files["package.json"]} if "package.json" in files else {})}
    request = build_request(plan, originals, sorted(tree))
    try:
        response = await llm.complete(request)
    except LlmUnavailable:
        return _rejected(plan, "TRANSFORM_UNAVAILABLE", f"{_summary(plan)} — 코드 패치(LLM)를 쓸 수 없다")
    except LlmRefused:
        return _rejected(plan, "TRANSFORM_REJECTED", f"{_summary(plan)} — 모델이 코드 패치를 거절했다",
                         [Issue("REFUSED", "", "모델 거절")])
    try:
        output = TransformOutput.model_validate_json(response.text)
    except ValidationError:
        return _rejected(plan, "TRANSFORM_REJECTED", f"{_summary(plan)} — 코드 패치 결과를 읽을 수 없었다",
                         [Issue("OUTPUT_INVALID", "", "LLM 출력이 코드 패치 스키마를 따르지 않았다")])
    changes, issues = check_patch(plan, output, originals, sources)
    if issues:
        codes = ", ".join(sorted({i.code for i in issues}))
        return _rejected(plan, "TRANSFORM_REJECTED", f"{_summary(plan)} — 코드 패치가 검사를 통과하지 못했다({codes})",
                         issues)
    whys = {n.path: redact(n.why) for n in output.notes}
    details = tuple({"path": p, "source": "코드 패치", "reason": whys.get(p) or ("삭제" if t is None else "변경")}
                    for p, t in sorted(changes.items()))
    return TransformOutcome("patched", "PATCHED", _summary(plan), plan, changes, details)


def apply_to_context(context: GenerationContext, findings: Sequence[Finding],
                     outcome: TransformOutcome) -> tuple[GenerationContext, tuple[Finding, ...]]:
    """변환한 코드에 맞춰 생성 문맥을 고친다 — DB 는 변환 결과, 볼륨은 필요 없고, 데이터는 남아야 한다.

    레포 분석이 SQLite 때문에 비워 둔 /database·/requirements 를 근거와 함께 채운다.
    """
    plan = outcome.plan
    if outcome.action != "patched" or plan.database is None:
        return context, tuple(findings)
    secrets = tuple(s for s in context.secrets or () if s.name not in {r.name for r in plan.secrets}) + plan.secrets
    context = context.model_copy(update={
        "database": plan.database, "secrets": secrets, "storage": context.storage or Storage(),
        "requirements": Requirements(persistence=True),
    })
    why = next(i.reason for i in plan.items if i.code == "DB_SQLITE_TO_POSTGRES")
    replaced = {"/database": Finding("/database", "코드 패치", why, True),
                "/requirements": Finding("/requirements", "코드 패치", "DB 데이터는 재배포 뒤에도 남아야 한다", True)}
    kept = tuple(f for f in findings if f.path not in replaced)
    return context, kept + tuple(replaced.values())
