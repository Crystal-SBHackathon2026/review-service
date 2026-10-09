"""출력 검증 게이트 — 코드가 LLM 출력을 검사한다 (환각 대책의 본체).

1. 스키마: JSON 이 LlmReview 이고, items 가 findings 와 1:1 이다
2. 인용: cited_rule_ids 가 검색된 문서에 있고, 자기 finding 의 rule_id 를 포함한다
3. 패치: 아래를 모두 통과해야 Patch 가 된다
   - 대상은 autofix allowed finding 만, 적용 전후로 실제 바뀐 필드가 대상 규칙의 허용 필드(RULE_PATCH_PATHS) 아래만
   - 실제 명세에 적용되고 형식(DeploySpec)을 통과한다
   - 엔진·배치 변경은 데이터가 없고, engine_policy 가 allow_convert 이거나 대상이 DB-002 일 때만
   - persistent 볼륨을 없애거나 비영속으로 바꾸거나 줄이지 않는다
   - DB 를 없애거나(engine none) 외부 DB 로 돌리지 않고, 버킷을 지우지 않으며, 보호 설정을 약하게 바꾸지 않는다
     (허용 필드를 넓혀도 "지워서 고친" 패치가 통과하지 않게 따로 둔다)
   - 비밀처럼 보이는 env 를 새로 넣지 않는다. env 는 SEC-004 대상 항목을 지우는 것 말고는 바꾸지 않는다
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
from review_ai.patching import PatchError, apply_ops, changed_paths, parse_pointer, path_under
from review_ai.secrets_pattern import MASK, looks_secret, redact
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


def _overlaps(path: str, pattern: str) -> bool:
    """op 경로가 허용 필드 아래이거나 그 조상인가 ('*' 는 아무 토큰). 조상 op 의 실제 효과는 _within 이 본다."""
    tokens, expected = parse_pointer(path), parse_pointer(pattern)
    return all(e in ("*", t) for e, t in zip(expected, tokens))


def _ops_in_scope(ops: Sequence[PatchOp], allowed: Sequence[str]) -> bool:
    """op 경로가 허용 필드와 겹치고, 값에 가린 값(MASK)이 없다.

    State 의 명세는 가린 사본일 수 있다. 가린 값을 그대로 다시 쓰는 op 는 여기서는 변화가 없어 보이지만,
    커밋 단계가 원본에 적용하면 실제 값을 ***MASKED*** 로 덮는다 — 그래서 바뀐 필드 검사(_within)와 따로 막는다.
    """
    return all(
        any(_overlaps(op["path"], pattern) for pattern in allowed)
        and MASK not in json.dumps(op.get("value"), ensure_ascii=False)
        for op in ops
    )


def _within(before: DeploySpec, after: DeploySpec, allowed: Sequence[str]) -> bool:
    """실제로 바뀐 말단 필드가 전부 허용 필드 아래인가. baseline 도 비교한다 — 허용 필드에 없으므로 바뀌면 걸린다."""
    changed = changed_paths(before.model_dump(mode="json"), after.model_dump(mode="json"))
    return all(any(path_under(path, pattern) for pattern in allowed) for path in changed)


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


def _keeps_database(before: DeploySpec, after: DeploySpec) -> bool:
    b, a = before.database, after.database
    return not (
        (b.engine != "none" and a.engine == "none")
        or (a.placement == "external" and b.placement != "external")
        or (a.publicly_accessible and not b.publicly_accessible)
        or a.backup_retention_days < b.backup_retention_days
    )


def _keeps_buckets(before: DeploySpec, after: DeploySpec) -> bool:
    remaining = {b.name: b for b in after.storage.buckets}
    for old in before.storage.buckets:
        new = remaining.get(old.name)
        if new is None or (new.public and not old.public):
            return False
        if (old.versioning and not new.versioning) or (old.encryption and not new.encryption):
            return False
    return True


def _adds_secret_env(before: DeploySpec, after: DeploySpec) -> bool:
    old = before.runtime.env
    return any(looks_secret(k, v) and old.get(k) != v for k, v in after.runtime.env.items())


def _env_only_drops_shadowed(before: DeploySpec, after: DeploySpec, targets: Sequence[Finding]) -> bool:
    """env 는 SEC-004 대상 항목을 지우는 것만 허용한다 — '/runtime/env/*' 허용 필드로 다른 env 값을 바꾸지 못하게."""
    shadowed = {f["location"]["spec_path"].rsplit("/", 1)[1] for f in targets if f["rule_id"] == "SEC-004"}
    old, new = before.runtime.env, after.runtime.env
    changed = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
    return all(k in shadowed and k not in new for k in changed)


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
    if ops is None or not _ops_in_scope(ops, allowed_paths):
        return None
    before = DeploySpec.model_validate(spec)
    try:
        after = DeploySpec.model_validate(apply_ops(spec, ops))
    except (PatchError, ValidationError, ValueError):
        return None
    guards = (
        _within(before, after, allowed_paths),
        _engine_change_ok(before, after, target_rules),
        _keeps_persistent_data(before, after),
        _keeps_database(before, after),
        _keeps_buckets(before, after),
        not _adds_secret_env(before, after),
        _env_only_drops_shadowed(before, after, [by_id[fid] for fid in targets]),
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
