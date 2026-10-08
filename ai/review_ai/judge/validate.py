"""출력 검증 게이트 — 코드가 LLM 출력을 검사한다 (환각 대책의 본체).

1. 스키마: JSON 이 LlmReview 이고, items 가 findings 와 1:1 이다
2. 인용: cited_rule_ids 가 검색된 문서에 있고, 자기 finding 의 rule_id 를 포함한다
3. 패치: 아래를 모두 통과해야 Patch 가 된다
   - 대상은 autofix allowed finding 만, ops 는 대상 규칙의 허용 경로(RULE_PATCH_PATHS) 아래만
   - 실제 명세에 적용되고 형식(DeploySpec)을 통과한다
   - 엔진·배치 변경은 데이터가 없고, engine_policy 가 allow_convert 이거나 대상이 DB-002 일 때만
   - persistent 볼륨을 없애거나 비영속으로 바꾸거나 줄이지 않는다
   - 비밀처럼 보이는 env 를 새로 넣지 않는다
   - 적용한 명세를 다시 검사하면 대상 finding 이 사라지고 새 finding 이 생기지 않는다
4. 자유 텍스트(why·extra_opinions)는 비밀처럼 보이는 부분을 가린다
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from review_ai.catalog import load_targets
from review_ai.judge.prompt import RULE_PATCH_PATHS
from review_ai.judge.schema import LlmPatch, LlmReview
from review_ai.overlay import overlay_diff
from review_ai.patching import PatchError, apply_ops
from review_ai.secrets_pattern import looks_secret, redact
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.state import Doc, Finding, Patch, PatchOp
from review_ai.static_check import run_static_check
from review_ai.static_check.context import CheckContext, size_mi
from review_ai.verdict import Validation

INVALID = Validation(llm_available=True, schema_ok=False, citations_ok=False, patch_scope_ok=False)


def _sanitized(review: LlmReview) -> LlmReview:
    items = [item.model_copy(update={"why": redact(item.why)}) for item in review.items]
    return review.model_copy(update={"items": items, "extra_opinions": [redact(o) for o in review.extra_opinions]})


def parse_review(text: str, findings: Sequence[Finding]) -> LlmReview | None:
    try:
        review = LlmReview.model_validate_json(text)
    except ValidationError:
        return None
    if sorted(i.finding_id for i in review.items) != sorted(f["finding_id"] for f in findings):
        return None
    return _sanitized(review)


def citations_ok(review: LlmReview, findings: Sequence[Finding], docs: Sequence[Doc]) -> bool:
    available = {d["rule_id"] for d in docs if d["rule_id"]}
    rule_of = {f["finding_id"]: f["rule_id"] for f in findings}
    return all(
        set(item.cited_rule_ids) <= available and rule_of[item.finding_id] in item.cited_rule_ids
        for item in review.items
    )


def _under(path: str, prefixes: Sequence[str]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _engine_change_ok(before: DeploySpec, after: DeploySpec, target_rules: set[str]) -> bool:
    b, a = before.database, after.database
    if (b.engine, b.placement) == (a.engine, a.placement):
        return True
    if CheckContext(before, load_targets()[before.target.env]).has_existing_data:
        return False
    return b.engine_policy == "allow_convert" or "DB-002" in target_rules


def _keeps_persistent_data(before: DeploySpec, after: DeploySpec) -> bool:
    remaining = {v.name: v for v in after.storage.volumes}
    for vol in before.storage.volumes:
        if not vol.persistent:
            continue
        kept = remaining.get(vol.name)
        if kept is None or not kept.persistent or size_mi(kept.size) < size_mi(vol.size):
            return False
    return True


def _adds_secret_env(before: DeploySpec, after: DeploySpec) -> bool:
    old = before.runtime.env
    return any(looks_secret(k, v) and old.get(k) != v for k, v in after.runtime.env.items())


def _resolves_without_new(before_findings: Sequence[Finding], after: DeploySpec, targets: set[str]) -> bool:
    after_ids = {f["finding_id"] for f in run_static_check(after)}
    before_ids = {f["finding_id"] for f in before_findings}
    return not (after_ids & targets) and after_ids <= before_ids


def _parse_ops(llm_patch: LlmPatch) -> list[PatchOp] | None:
    try:
        return [op.to_patch_op() for op in llm_patch.ops]
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None


def build_patch(llm_patch: LlmPatch, findings: Sequence[Finding], spec: dict[str, Any]) -> Patch | None:
    """3번 조건을 하나라도 어기면 None. 통과하면 ops 와 overlay diff 를 담은 Patch."""
    by_id = {f["finding_id"]: f for f in findings}
    targets = set(llm_patch.target_finding_ids)
    if not targets <= {fid for fid, f in by_id.items() if f["autofix"] == "allowed"}:
        return None
    target_rules = {by_id[fid]["rule_id"] for fid in targets}
    allowed_paths = [p for rule in target_rules for p in RULE_PATCH_PATHS.get(rule, ())]
    ops = _parse_ops(llm_patch)
    if ops is None or not all(_under(op["path"], allowed_paths) for op in ops):
        return None
    before = DeploySpec.model_validate(spec)
    try:
        after = DeploySpec.model_validate(apply_ops(spec, ops))
    except (PatchError, ValidationError, ValueError):
        return None
    guards = (
        _engine_change_ok(before, after, target_rules),
        _keeps_persistent_data(before, after),
        not _adds_secret_env(before, after),
        _resolves_without_new(findings, after, targets),
    )
    if not all(guards):
        return None
    return Patch(kind="config", ops=ops, target_finding_ids=sorted(targets), files=overlay_files(before, after))


def overlay_files(before: DeploySpec, after: DeploySpec) -> list[dict[str, str]]:
    """State 의 명세가 Kafka 의 가린 사본이면 overlay 를 만들 수 없다 — diff 없이 ops 만 넘기고,
    커밋 단계가 spec_ref 로 읽은 원본에 ops 를 적용해 render_overlay 한다."""
    try:
        return overlay_diff(before, after)
    except ValueError:
        return []


def validate_output(
    text: str, findings: Sequence[Finding], docs: Sequence[Doc], spec: dict[str, Any]
) -> tuple[LlmReview | None, Validation, Patch | None]:
    review = parse_review(text, findings)
    if review is None:
        return None, INVALID, None
    patch = build_patch(review.patch, findings, spec) if review.patch else None
    validation = Validation(
        llm_available=True,
        schema_ok=True,
        citations_ok=citations_ok(review, findings, docs),
        patch_scope_ok=review.patch is None or patch is not None,
    )
    return review, validation, patch
