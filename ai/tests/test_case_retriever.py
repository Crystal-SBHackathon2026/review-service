"""사례 검색 — rule_id 로 사례를 근거 문서로, 저장소 오류는 검토를 멈추지 않는다, 여러 검색기 잇기."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from review_ai.graph import initial_state, run_graph
from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.retrieval.case_retriever import CaseRetriever, CompositeRetriever, case_doc
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.state import Doc
from tests.conftest import load_sample_dict


def case(case_id: str, rule_ids: list[str], target_env: str = "aws") -> dict[str, Any]:
    return {"case_id": case_id, "review_id": case_id.split(".")[0], "app": "sample-app", "target_env": target_env,
            "rule_ids": rule_ids, "outcome": "rejected", "summary": f"지난 검토 {case_id}", "ops": []}


class Source:
    def __init__(self, cases: list[dict[str, Any]], *, error: Exception | None = None) -> None:
        self.cases, self.error = cases, error
        self.calls: list[tuple[str, str, int]] = []

    async def find_cases(self, rule_id: str, *, target_env: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append((rule_id, target_env, limit))
        if self.error:
            raise self.error
        return [c for c in self.cases if rule_id in c["rule_ids"]][:limit]


def finding(rule_id: str) -> dict[str, Any]:
    return {"finding_id": rule_id, "rule_id": rule_id, "severity": "high", "title": rule_id,
            "location": {"spec_path": "/x"}, "evidence": ""}


async def test_cases_become_exact_rule_docs_once_per_case() -> None:
    source = Source([case("rv_1.rejected", ["DB-001", "STO-005"]), case("rv_2.recommended", ["DB-001"], "gcp")])

    docs = await CaseRetriever(source).search([finding("STO-005"), finding("DB-001"), finding("DB-001")], "aws")

    assert [(d["chunk_id"], d["rule_id"]) for d in docs] == [("case:rv_1.rejected", "DB-001"),
                                                            ("case:rv_2.recommended", "DB-001")]
    assert source.calls == [("DB-001", "aws", 3), ("STO-005", "aws", 3)]
    assert docs[1] == Doc(chunk_id="case:rv_2.recommended", rule_id="DB-001", doc_type="case", provider="gcp",
                          score=1.0, match="exact_rule", source_uri="review://rv_2", text="지난 검토 rv_2.recommended")


async def test_store_error_returns_no_cases(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        docs = await CaseRetriever(Source([], error=RuntimeError("db down"))).search([finding("DB-001")], "aws")

    assert docs == []
    assert "사례 검색 실패" in caplog.text


async def test_composite_keeps_order_and_drops_duplicate_chunks() -> None:
    first = Source([case("rv_1.rejected", ["DB-001"])])
    rules = FileRetriever()
    composite = CompositeRetriever(rules, CaseRetriever(first), CaseRetriever(first))

    docs = await composite.search([finding("DB-001")], "aws")

    rule_docs = await rules.search([finding("DB-001")], "aws")
    assert docs == [*rule_docs, case_doc(case("rv_1.rejected", ["DB-001"]), "DB-001")]


async def test_judge_sees_case_and_citation_still_valid() -> None:
    spec = load_sample_dict("06-human-plaintext-secret.yaml")
    prompts: list[str] = []

    def recording(request: Any) -> dict[str, Any]:
        prompts.append(request.user)
        return oracle_review(request)

    retriever = CompositeRetriever(FileRetriever(), CaseRetriever(Source([case("rv_old.rejected", ["SEC-001"])])))
    final = await run_graph(initial_state(spec, review_id="rv_new"), llm=ScriptedLLM(recording), retriever=retriever)

    assert "지난 검토 rv_old.rejected" in prompts[0]
    assert '"doc_type": "case"' in prompts[0]
    assert final["decision"]["validation"]["citations_ok"]
