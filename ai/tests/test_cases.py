"""판단 사례 — 종료 방식, 남길 ops(비밀 경로·값 제외), 요약, State·업무 DB 행에서 만들기."""

from __future__ import annotations

from typing import Any

import pytest

from review_ai.cases import OUTCOME_TEXT, build_case, case_from_review, case_from_state, case_ops, case_outcome
from review_ai.graph import initial_state, run_graph
from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.retrieval.file_retriever import FileRetriever
from tests.conftest import load_sample_dict

SECRET = "sample-not-a-real-token-0000"


def finding(rule_id: str = "DB-001", severity: str = "high", path: str = "/database/engine",
            evidence: str = "mysql") -> dict[str, Any]:
    return {"finding_id": f"{rule_id}:{path}", "rule_id": rule_id, "severity": severity, "title": f"{rule_id} 제목",
            "location": {"spec_path": path}, "evidence": evidence}


ENGINE = {"op": "replace", "path": "/database/engine", "value": "postgres"}
VERSION = {"op": "add", "path": "/database/version", "value": "16"}


@pytest.mark.parametrize(("human", "outcome"), [
    (None, "auto_fixed"),
    ({"decision": "rejected", "edited_ops": []}, "rejected"),
    ({"decision": "approved", "edited_ops": []}, "human_approved"),
    ({"decision": "approved", "edited_ops": [ENGINE, VERSION], "defaulted_ops": [VERSION]}, "human_edited"),
    ({"decision": "approved", "edited_ops": [ENGINE, VERSION], "defaulted_ops": [ENGINE, VERSION]}, "recommended"),
])
def test_case_outcome(human: dict[str, Any] | None, outcome: str) -> None:
    assert case_outcome(human) == outcome


@pytest.mark.parametrize("op", [
    {"op": "add", "path": "/runtime/env/LOG_LEVEL", "value": "debug"},   # env 는 값이 무엇이든 남기지 않는다
    {"op": "replace", "path": "/runtime", "value": {"port": 8080, "env": {"DB_PASSWORD": "x"}}},  # env 를 품은 통째 교체
    {"op": "add", "path": "/secrets/0", "value": {"name": "DB_PASSWORD", "source": "k8s-secret", "key": "pw"}},
    {"op": "replace", "path": "/baseline/spec", "value": {}},
    {"op": "replace", "path": "", "value": {}},
    {"op": "replace", "path": "/network/ingress/host", "value": "ghp_" + "a" * 36},  # 값이 비밀처럼 보인다
])
def test_case_ops_drop_private_paths_and_secret_values(op: dict[str, Any]) -> None:
    assert case_ops([op, ENGINE]) == [ENGINE]


def test_build_case_summary_has_rules_paths_values_but_not_evidence() -> None:
    case = build_case(review_id="rv_1", app="orders", target_env="aws", outcome="recommended",
                      rounds=[{"findings": [finding(evidence=SECRET), finding("NET-001", "low", "/network")],
                               "reasons": ["IRREVERSIBLE"]}],
                      findings=[finding(evidence=SECRET)], reasons=["IRREVERSIBLE"], ops=[ENGINE, VERSION])

    assert case is not None
    assert (case["case_id"], case["rule_ids"], case["ops"]) == ("rv_1.recommended", ["DB-001"], [ENGINE, VERSION])
    summary = case["summary"]
    assert OUTCOME_TEXT["recommended"] in summary and "(orders, aws)" in summary
    assert "DB-001 DB-001 제목 (/database/engine)" in summary
    assert '/database/engine = "postgres", /database/version = "16"' in summary
    assert "사람 확인 사유: IRREVERSIBLE" in summary
    assert SECRET not in summary and "NET-001" not in summary  # evidence·low 경고는 넣지 않는다


def test_build_case_without_decided_findings_is_none() -> None:
    assert build_case(review_id="rv_1", app="a", target_env="aws", outcome="auto_fixed", rounds=[],
                      findings=[finding("NET-001", "low", "/network")], reasons=[], ops=[]) is None


def test_rejected_case_keeps_no_ops_and_unknown_outcome_raises() -> None:
    case = build_case(review_id="rv_1", app="a", target_env="aws", outcome="rejected", rounds=[],
                      findings=[finding()], reasons=[], ops=[ENGINE])
    assert case is not None and case["ops"] == [] and "적용한 값" not in case["summary"]
    with pytest.raises(ValueError, match="모르는"):
        build_case(review_id="rv_1", app="a", target_env="aws", outcome="merged", rounds=[], findings=[finding()],
                   reasons=[], ops=[])


def test_long_values_are_cut_in_summary() -> None:
    op = {"op": "replace", "path": "/network/ingress/host", "value": "h" * 200}
    case = build_case(review_id="rv_1", app="a", target_env="aws", outcome="human_edited", rounds=[],
                      findings=[finding()], reasons=[], ops=[op])
    assert case is not None and "h" * 200 not in case["summary"] and "…" in case["summary"]


async def test_case_from_state_after_autofix() -> None:
    spec = load_sample_dict("03-fix-sqlite-replicas-gcp.yaml")
    final = await run_graph(initial_state(spec, review_id="rv_fix"), llm=ScriptedLLM(oracle_review),
                            retriever=FileRetriever())

    case = case_from_state(final)

    assert case is not None
    assert (case["outcome"], case["app"], case["target_env"], case["rule_ids"]) == (
        "auto_fixed", spec["metadata"]["name"], "gcp", ["DB-003"])
    assert case["ops"] == final["applied_ops"]


async def test_case_from_state_with_rejection_given_separately() -> None:
    spec = load_sample_dict("06-human-plaintext-secret.yaml")
    final = await run_graph(initial_state(spec, review_id="rv_sec"), llm=ScriptedLLM(oracle_review),
                            retriever=FileRetriever())

    case = case_from_state(final, human={"decision": "rejected", "approver": "a", "edited_ops": []})

    assert case is not None and case["outcome"] == "rejected" and case["rule_ids"] == ["SEC-001"]
    assert SECRET not in case["summary"]


def test_case_from_review_row() -> None:
    row = {"review_id": "rv_db", "app": "orders", "target_env": "local", "findings": [],
           "rounds": [{"findings": [finding()], "reasons": [], "patch": {"ops": [ENGINE]}}],
           "decision": {"reasons": []}}

    case = case_from_review(row, "deploy_degraded")

    assert case is not None and (case["case_id"], case["ops"]) == ("rv_db.deploy_degraded", [ENGINE])
