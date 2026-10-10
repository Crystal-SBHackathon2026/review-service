"""값이 하나로 정해지는 허용 규칙만 처리한다. 추천값과 동일한 후보를 기존 패치 게이트로 검증한다."""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from review_ai.judge.schema import LlmItem, LlmPatch, LlmPatchOp, LlmReview
from review_ai.judge.validate import build_patch, citations_ok
from review_ai.recommendations import build_recommendations
from review_ai.state import Doc, Finding, Patch

RULES = frozenset({"DB-006", "STO-003", "STO-004"})
VERSION = "deterministic-v1"


def deterministic_review(spec: dict[str, Any], findings: list[Finding], docs: list[Doc]) -> tuple[LlmReview, Patch] | None:
    counted = [f for f in findings if f["severity"] != "low"]
    if not counted or any(f["rule_id"] not in RULES or f["autofix"] != "allowed" for f in counted):
        return None
    eligible = {f["finding_id"] for f in counted}
    candidates = [c for c in build_recommendations(spec, findings) if c["source"] == "rule"
                  and set(c["finding_ids"]) <= eligible]
    covered = {fid for c in candidates for fid in c["finding_ids"]}
    if covered != {f["finding_id"] for f in counted}:
        return None
    try:
        proposal = LlmPatch(ops=[LlmPatchOp(op=op["op"], path=op["path"], value_json=json.dumps(op["value"]))
                                for c in candidates for op in c["ops"]], target_finding_ids=sorted(covered))
    except ValidationError:
        return None
    patch = build_patch(proposal, findings, spec)
    if patch is None:
        return None
    why = {fid: c["why"] for c in candidates for fid in c["finding_ids"]}
    items = [LlmItem(finding_id=f["finding_id"], cited_rule_ids=[f["rule_id"]],
                     cited_chunk_ids=[d["chunk_id"] for d in docs
                                      if d["doc_type"] == "rule" and d["rule_id"] == f["rule_id"]][:1],
                     why=f"{f['title']}. {why[f['finding_id']]}" if f["finding_id"] in why else f"경고: {f['title']}",
                     fix_kind="config" if f["finding_id"] in covered else "none") for f in findings]
    review = LlmReview(items=items, patch=proposal)
    return (review, patch) if citations_ok(review, findings, docs) else None
