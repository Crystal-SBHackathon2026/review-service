from __future__ import annotations

from typing import Any

import pytest

from review_ai.evaluation import build_spec, load_eval_cases, run_case, summarize
from review_ai.graph import initial_state, run_graph
from review_ai.judge.fake_llm import FAKES
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


@pytest.mark.parametrize("case", load_eval_cases(), ids=lambda c: c["id"])
async def test_applied_ops_reproduce_final_spec(case: dict[str, Any]) -> None:
    spec = build_spec(case)
    final = await run_graph(initial_state(spec, review_id="t"), llm=FAKES["oracle"](), retriever=FileRetriever())
    assert apply_ops(spec, final["applied_ops"]) == final["deploy_spec"]
    assert final["patched"] == bool(final["rounds"])
