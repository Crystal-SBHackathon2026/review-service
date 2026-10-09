from __future__ import annotations

from typing import Any

import pytest
import yaml

from review_ai.evaluation import (
    INTAKE_REJECTED, _check_intake, _Run, build_spec, intake_kind, intake_text, load_eval_cases, run_case, summarize,
)
from review_ai.graph import initial_state, run_graph
from review_ai.intake import IntakeOutcome
from review_ai.intake.fake_repair import REPAIR_FAKES
from review_ai.judge.fake_llm import FAKES, ScriptedLLM
from review_ai.patching import apply_ops
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import run_static_check
from tests.conftest import load_sample_dict


@pytest.mark.parametrize("case", load_eval_cases(), ids=lambda c: c["id"])
async def test_eval_case_with_fake_reviewer(case: dict[str, Any]) -> None:
    result = await run_case(case, FAKES["oracle"], FileRetriever())
    assert result.ok, result.failures


async def test_summary_metrics_all_green() -> None:
    results = [await run_case(c, FAKES["oracle"], FileRetriever()) for c in load_eval_cases()]
    summary = summarize(results)
    assert summary["match_rate"] == 1.0
    assert summary["citation_valid_rate"] == 1.0
    assert summary["false_positive_on_pass"] == 0


async def test_fix_loop_records_rounds_and_patched_overlay() -> None:
    llm = FAKES["oracle"]()
    final = await run_graph(initial_state(load_sample_dict("03-fix-sqlite-replicas-gcp.yaml"), review_id="t"),
                            llm=llm, retriever=FileRetriever())
    assert final["status"] == "pass"
    assert final["deploy_spec"]["runtime"]["replicas"] == 1
    [snap] = final["rounds"]
    assert snap["verdict"] == "fix" and snap["patch"]["ops"] == [{"op": "replace", "path": "/runtime/replicas", "value": 1}]
    [diff] = snap["patch"]["files"]
    assert diff["path"] == "apps/todo/overlays/gcp/kustomization.yaml" and "+      value: 1" in diff["diff"]
    assert llm.calls == 1


async def test_without_llm_static_results_survive() -> None:
    final = await run_graph(initial_state(load_sample_dict("10-human-mixed-aws.yaml"), review_id="t"),
                            llm=None, retriever=FileRetriever())
    assert final["decision"]["reasons"] == ["AUTOFIX_FORBIDDEN", "LLM_UNAVAILABLE"]
    assert [f["rule_id"] for f in final["findings"]] == ["RUN-001", "STO-003"]


async def test_fix_loop_exposes_applied_ops_for_commit_stage() -> None:
    original = load_sample_dict("03-fix-sqlite-replicas-gcp.yaml")
    final = await run_graph(initial_state(original, review_id="t"), llm=FAKES["oracle"](), retriever=FileRetriever())
    assert (final["status"], final["patch"], final["patched"]) == ("pass", None, True)
    assert final["applied_ops"] == [{"op": "replace", "path": "/runtime/replicas", "value": 1}]
    committed = apply_ops(original, final["applied_ops"])  # 커밋 단계가 spec_ref 원본에 하는 일
    assert run_static_check(DeploySpec.model_validate(committed)) == []
    assert original["runtime"]["replicas"] != 1  # 입력은 바뀌지 않는다


@pytest.mark.parametrize("name", ["01-pass-sample-app-aws.yaml", "02-pass-local-sqlite.yaml"])
async def test_pass_without_fix_has_no_applied_ops(name: str) -> None:
    final = await run_graph(initial_state(load_sample_dict(name), review_id="t"),
                            llm=FAKES["oracle"](), retriever=FileRetriever())
    assert (final["status"], final["applied_ops"], final["patched"]) == ("pass", [], False)


@pytest.mark.parametrize("case", [c for c in load_eval_cases() if "intake" not in c], ids=lambda c: c["id"])
async def test_applied_ops_reproduce_final_spec(case: dict[str, Any]) -> None:
    spec = build_spec(case)
    final = await run_graph(initial_state(spec, review_id="t"), llm=FAKES["oracle"](), retriever=FileRetriever())
    assert apply_ops(spec, final["applied_ops"]) == final["deploy_spec"]
    assert final["patched"] == bool(final["rounds"])


# ── intake·resume 케이스 실행기 ─────────────────────────────────────────


def _intake_case(**intake: Any) -> dict[str, Any]:
    return {"id": "t", "llm": "reviewer", "intake": {"repo": "sample-app", **intake},
            "expect": {"verdict": "pass", "preserves_sample": True}}


def test_intake_edit_must_hit_sample_exactly_once() -> None:
    case = _intake_case(sample="01-pass-sample-app-aws.yaml", edit=[["no-such-text", "x"]])
    with pytest.raises(ValueError, match="정확히 한 번"):
        intake_text(case)


@pytest.mark.parametrize(("text", "kind"), [
    (None, "missing"), ("", "empty"), ("# 주석\n", "empty"), ("{}", "empty"),
    ("a: [1", "yaml_error"), ("a: 1", "schema_error"),
])
def test_intake_kind_matches_review_api(text: str | None, kind: str) -> None:
    assert intake_kind(text) == kind


async def test_intake_case_that_is_still_valid_is_a_broken_case() -> None:
    case = _intake_case(sample="01-pass-sample-app-aws.yaml", edit=[["replicas: 2", "replicas: 3"]])
    with pytest.raises(ValueError, match="정상 명세"):
        await run_case(case, FAKES["oracle"], FileRetriever())


def test_preserves_sample_catches_a_different_repair() -> None:
    case = _intake_case(sample="01-pass-sample-app-aws.yaml")
    other = build_spec({"sample": "06-human-plaintext-secret.yaml"})
    outcome = IntakeOutcome("repaired", "REPAIRED", "m", content=yaml.safe_dump(other))

    assert _check_intake(case, _Run(spec=None, final=None, outcome=outcome)) == ["복구한 명세가 원래 샘플과 다르다"]


def _repaired_target(**target: str) -> IntakeOutcome:
    spec = build_spec({"sample": "01-pass-sample-app-aws.yaml"})
    return IntakeOutcome("repaired", "REPAIRED", "m", content=yaml.safe_dump({**spec, "target": target}))


def test_preserves_sample_accepts_omitted_namespace_that_defaults_to_the_same() -> None:
    """namespace 생략은 metadata.name 과 같다 — 실제 복구 LLM 이 가끔 빼도 배포 결과는 같다."""
    case = _intake_case(sample="01-pass-sample-app-aws.yaml")
    same = _repaired_target(env="aws", region="ap-northeast-2")
    other = _repaired_target(env="aws", region="ap-northeast-2", namespace="other-ns")

    assert _check_intake(case, _Run(spec=None, final=None, outcome=same)) == []
    assert _check_intake(case, _Run(spec=None, final=None, outcome=other)) == ["복구한 명세가 원래 샘플과 다르다"]


async def test_rejected_intake_is_not_reviewed() -> None:
    case = next(c for c in load_eval_cases() if c["id"] == "e08-repair-invents")
    result = await run_case(case, FAKES["oracle"], FileRetriever())

    assert (result.verdict, result.reasons, result.llm_role) == (INTAKE_REJECTED, ["REPAIR_REJECTED"],
                                                                 "repair:invents_value")
    assert result.llm_calls == 1  # 복구 한 번 — 검토(judge)는 돌지 않는다


async def test_repairer_replaces_oracle_for_reviewer_role() -> None:
    case = next(c for c in load_eval_cases() if c["id"] == "e03-yaml-syntax")
    result = await run_case(case, FAKES["oracle"], FileRetriever(), REPAIR_FAKES["changes_value"])

    assert not result.ok and result.verdict == INTAKE_REJECTED


BROKEN = "api_version: crystal.review/v1alpha1\nkind: [DeploySpec\n"


async def test_raw_broken_case_reaches_the_given_repairer() -> None:
    """raw 원문도 복구 자리(실제 Claude)를 탄다 — 예전엔 '사용 불가' 가짜로 조용히 바뀌었다."""
    calls = []
    repairer = lambda _answer: ScriptedLLM(lambda r: calls.append(r) or {"spec_json": "{}", "changes": []})  # noqa: E731
    case = {**_intake_case(raw=BROKEN), "expect": {"verdict": INTAKE_REJECTED, "intake": "rejected"}}

    result = await run_case(case, FAKES["oracle"], FileRetriever(), repairer)

    assert result.ok, result.failures
    assert len(calls) == 1 and result.llm_calls == 1 and result.reasons == ["REPAIR_REJECTED"]


async def test_raw_broken_case_with_fake_repair_needs_a_sample() -> None:
    with pytest.raises(ValueError, match="sample"):
        await run_case(_intake_case(raw=BROKEN), FAKES["oracle"], FileRetriever())


async def test_generated_case_needs_no_answer() -> None:
    case = next(c for c in load_eval_cases() if c["id"] == "e01-missing")
    result = await run_case(case, FAKES["oracle"], FileRetriever(), REPAIR_FAKES["changes_value"])

    assert result.ok and result.llm_calls == 0  # 복구 LLM 을 만들지도 부르지도 않는다


async def test_resume_on_a_review_that_did_not_pause_fails() -> None:
    case = {"id": "t", "sample": "01-pass-sample-app-aws.yaml", "llm": "reviewer", "resume": {"edited_ops": []},
            "expect": {"verdict": "pass"}}

    result = await run_case(case, FAKES["oracle"], FileRetriever())

    assert not result.ok and "멈추지 않았다" in result.failures[0]


def test_preserves_sample_without_repair_result_fails() -> None:
    case = _intake_case(sample="01-pass-sample-app-aws.yaml")
    outcome = IntakeOutcome("rejected", "REPAIR_REJECTED", "m")

    assert _check_intake(case, _Run(spec=None, final=None, outcome=outcome)) == ["비교할 복구 결과나 정답 샘플이 없다"]
