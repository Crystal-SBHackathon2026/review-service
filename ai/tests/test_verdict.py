from __future__ import annotations

import copy
from typing import Any

import pytest

from review_ai.judge.schema import LlmItem, LlmPatch, LlmPatchOp, LlmReview
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.state import Doc, Finding
from review_ai.static_check import run_static_check
from review_ai.verdict import MAX_PATCH_ROUNDS, Validation, applied_ops, decide_verdict, round_snapshot
from tests.conftest import load_cases, load_sample_dict

OK = Validation(llm_available=True, schema_ok=True, citations_ok=True, patch_scope_ok=True)


def findings_for(name: str) -> list[Finding]:
    return run_static_check(DeploySpec.model_validate(load_sample_dict(name)))


def exact_docs(findings: list[Finding]) -> list[Doc]:
    return [
        Doc(chunk_id=f"{f['rule_id']}#0", rule_id=f["rule_id"], doc_type="rule", provider="any",
            score=1.0, match="exact_rule", source_uri=f"rules/any/{f['rule_id']}.md", text="")
        for f in findings
    ]


def good_review(findings: list[Finding]) -> LlmReview:
    allowed = [f["finding_id"] for f in findings if f["autofix"] == "allowed"]
    patch = LlmPatch(ops=[LlmPatchOp(op="replace", path="/runtime/replicas", value_json="1")],
                     target_finding_ids=allowed) if allowed else None
    items = [LlmItem(finding_id=f["finding_id"], cited_rule_ids=[f["rule_id"]], why="설명", fix_kind="config")
             for f in findings]
    return LlmReview(items=items, patch=patch)


def decide(findings: list[Finding], **kw: Any):
    return decide_verdict(findings, kw.pop("docs", exact_docs(findings)), kw.pop("llm_out", good_review(findings)),
                          kw.pop("validation", OK), **kw)


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["file"])
def test_cases_yaml_first_round_verdicts(case: dict[str, Any]) -> None:
    decision = decide(findings_for(case["file"]))
    assert (decision["verdict"], decision["reasons"]) == (case["verdict"], case["reasons"])


def test_no_findings_passes_without_llm() -> None:
    decision = decide_verdict([], [], None, Validation(llm_available=False))
    assert (decision["verdict"], decision["reasons"], decision["llm"]) == ("pass", [], None)


def test_low_only_passes_even_if_llm_unavailable() -> None:
    findings = findings_for("01-pass-sample-app-aws.yaml")
    decision = decide_verdict(findings, [], None, Validation(llm_available=False))
    assert decision["verdict"] == "pass"


def test_llm_unavailable_with_real_findings() -> None:
    findings = findings_for("03-fix-sqlite-replicas-gcp.yaml")
    decision = decide_verdict(findings, exact_docs(findings), None, Validation(llm_available=False))
    assert (decision["verdict"], decision["reasons"]) == ("needs_human", ["LLM_UNAVAILABLE"])


@pytest.mark.parametrize("field", ["schema_ok", "citations_ok"])
def test_invalid_llm_output_is_citation_invalid(field: str) -> None:
    findings = findings_for("03-fix-sqlite-replicas-gcp.yaml")
    decision = decide(findings, validation={**OK, field: False})
    assert (decision["verdict"], decision["reasons"]) == ("needs_human", ["CITATION_INVALID"])


def test_citation_invalid_blocks_even_low_only() -> None:
    findings = findings_for("01-pass-sample-app-aws.yaml")
    decision = decide(findings, validation={**OK, "citations_ok": False})
    assert decision["verdict"] == "needs_human"


def test_patch_out_of_scope() -> None:
    findings = findings_for("07-fix-public-bucket.yaml")
    decision = decide(findings, validation={**OK, "patch_scope_ok": False})
    assert decision["reasons"] == ["PATCH_OUT_OF_SCOPE"]


def test_patch_missing_for_allowed_finding() -> None:
    findings = findings_for("07-fix-public-bucket.yaml")
    review = good_review(findings).model_copy(update={"patch": None})
    decision = decide(findings, llm_out=review)
    assert (decision["verdict"], decision["reasons"]) == ("needs_human", ["PATCH_MISSING"])


def test_low_score_when_no_exact_doc_and_weak_semantic_match() -> None:
    findings = findings_for("07-fix-public-bucket.yaml")
    weak = [Doc(chunk_id="g#0", rule_id=None, doc_type="guide", provider="any", score=0.2,
                match="semantic", source_uri="guides/x.md", text="")]
    decision = decide(findings, docs=weak)
    assert "LOW_SCORE" in decision["reasons"]


def test_loop_exhausted_after_max_rounds() -> None:
    findings = findings_for("07-fix-public-bucket.yaml")
    rounds = [{"finding_ids": ["other"]}] * MAX_PATCH_ROUNDS
    assert decide(findings, rounds=rounds)["reasons"] == ["LOOP_EXHAUSTED"]


def test_loop_stops_early_when_findings_do_not_shrink() -> None:
    findings = findings_for("07-fix-public-bucket.yaml")
    rounds = [{"finding_ids": [f["finding_id"] for f in findings]}]
    assert decide(findings, rounds=rounds)["reasons"] == ["LOOP_EXHAUSTED"]


def test_extra_opinions_do_not_change_verdict() -> None:
    findings = findings_for("01-pass-sample-app-aws.yaml")
    review = good_review(findings).model_copy(update={"extra_opinions": ["DB 를 postgres 로 바꾸세요"]})
    decision = decide(findings, llm_out=review)
    assert (decision["verdict"], decision["extra_opinions"]) == ("pass", ["DB 를 postgres 로 바꾸세요"])


def test_round_snapshot_shape() -> None:
    findings = findings_for("07-fix-public-bucket.yaml")
    decision = decide(findings)
    snap = round_snapshot({"findings": findings, "decision": decision, "patch": None, "retry_count": 0})
    assert snap == {"round": 0, "finding_ids": [findings[0]["finding_id"]], "findings": findings, "verdict": "fix",
                    "reasons": [], "items": decision["items"], "doc_ids": [], "patch": None}
    assert snap["findings"] is not findings  # 이후 State 변경이 기록에 번지지 않게 복사


def _op(path: str, value: Any) -> dict[str, Any]:
    return {"op": "replace", "path": path, "value": value}


def test_applied_ops_concatenates_rounds_in_order_then_pending_fix() -> None:
    state = {
        "rounds": [{"patch": {"ops": [_op("/a", 1), _op("/b", 2)]}}, {"patch": {"ops": [_op("/c", 3)]}}],
        "decision": {"verdict": "fix"},
        "patch": {"ops": [_op("/d", 4)]},
    }
    before = copy.deepcopy(state)
    ops = applied_ops(state)
    assert [op["path"] for op in ops] == ["/a", "/b", "/c", "/d"]
    ops[0]["value"] = 99
    assert state == before  # 입력 State 는 그대로


@pytest.mark.parametrize("verdict", ["pass", "needs_human"])
def test_applied_ops_ignores_patch_unless_verdict_is_fix(verdict: str) -> None:
    state = {"rounds": [{"patch": {"ops": [_op("/a", 1)]}}], "decision": {"verdict": verdict},
             "patch": {"ops": [_op("/z", 0)]}}
    assert applied_ops(state) == [_op("/a", 1)]


def test_applied_ops_empty_without_rounds_or_patch() -> None:
    assert applied_ops({"rounds": [], "decision": {"verdict": "pass"}, "patch": None}) == []
    assert applied_ops({}) == []
