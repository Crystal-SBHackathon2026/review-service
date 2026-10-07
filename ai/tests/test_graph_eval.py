from __future__ import annotations

from typing import Any

import pytest

from review_ai.evaluation import load_eval_cases, run_case, summarize
from review_ai.graph import initial_state, run_graph
from review_ai.judge.fake_llm import FAKES
from review_ai.retrieval.file_retriever import FileRetriever
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
