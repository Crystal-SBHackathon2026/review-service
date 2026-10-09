"""사례 검색 — rule_id 로 사례를 근거 문서로, 같은 앱·레포로 한정, 승인 사례 신뢰 플래그, 저장소 오류는 검토를 멈추지 않는다, 여러 검색기 잇기."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from review_ai.graph import initial_state, run_graph
from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.cases import OUTCOME_TEXT
from review_ai.retrieval import Scope, scope_of
from review_ai.retrieval.case_retriever import (APPROVAL_OUTCOMES, TRUST_APPROVALS_ENV, CaseRetriever,
                                                CompositeRetriever, case_doc, trusted_outcomes)
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.state import Doc
from tests.conftest import load_sample_dict


REPO = "Crystal-SBHackathon2026/sample-app"
SCOPE = Scope(app="sample-app", repository=REPO)


def case(case_id: str, rule_ids: list[str], target_env: str = "aws") -> dict[str, Any]:
    return {"case_id": case_id, "review_id": case_id.split(".")[0], "app": "sample-app", "target_env": target_env,
            "rule_ids": rule_ids, "outcome": case_id.split(".")[1], "summary": f"지난 검토 {case_id}", "ops": []}


class Source:
    """저장소 대역 — 앱·레포 필터는 저장소 몫이라 여기선 받은 값만 기록하고, outcomes 는 걸러 준다."""

    def __init__(self, cases: list[dict[str, Any]], *, error: Exception | None = None) -> None:
        self.cases, self.error = cases, error
        self.calls: list[dict[str, Any]] = []

    async def find_cases(self, rule_id: str, *, app: str, repository: str, target_env: str,
                         outcomes: Any, limit: int) -> list[dict[str, Any]]:
        self.calls.append({"rule_id": rule_id, "app": app, "repository": repository, "target_env": target_env,
                           "outcomes": tuple(outcomes), "limit": limit})
        if self.error:
            raise self.error
        return [c for c in self.cases if rule_id in c["rule_ids"] and c["outcome"] in outcomes][:limit]


def finding(rule_id: str) -> dict[str, Any]:
    return {"finding_id": rule_id, "rule_id": rule_id, "severity": "high", "title": rule_id,
            "location": {"spec_path": "/x"}, "evidence": ""}


async def test_cases_become_exact_rule_docs_once_per_case() -> None:
    source = Source([case("rv_1.rejected", ["DB-001", "STO-005"]), case("rv_2.recommended", ["DB-001"], "gcp")])

    retriever = CaseRetriever(source, outcomes=tuple(OUTCOME_TEXT))
    docs = await retriever.search([finding("STO-005"), finding("DB-001"), finding("DB-001")], "aws", SCOPE)

    assert [(d["chunk_id"], d["rule_id"]) for d in docs] == [("case:rv_1.rejected", "DB-001"),
                                                            ("case:rv_2.recommended", "DB-001")]
    assert [(c["rule_id"], c["app"], c["repository"], c["target_env"], c["limit"]) for c in source.calls] == [
        ("DB-001", "sample-app", REPO, "aws", 3), ("STO-005", "sample-app", REPO, "aws", 3)]
    assert docs[1] == Doc(chunk_id="case:rv_2.recommended", rule_id="DB-001", doc_type="case", provider="gcp",
                          score=1.0, match="exact_rule", source_uri="review://rv_2", text="지난 검토 rv_2.recommended")


async def test_store_error_returns_no_cases(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        docs = await CaseRetriever(Source([], error=RuntimeError("db down"))).search([finding("DB-001")], "aws", SCOPE)

    assert docs == []
    assert "사례 검색 실패" in caplog.text


async def test_composite_keeps_order_and_drops_duplicate_chunks() -> None:
    first = Source([case("rv_1.rejected", ["DB-001"])])
    rules = FileRetriever()
    composite = CompositeRetriever(rules, CaseRetriever(first), CaseRetriever(first))

    docs = await composite.search([finding("DB-001")], "aws", SCOPE)

    rule_docs = await rules.search([finding("DB-001")], "aws")
    assert docs == [*rule_docs, case_doc(case("rv_1.rejected", ["DB-001"]), "DB-001")]


async def test_judge_sees_case_and_citation_still_valid() -> None:
    spec = load_sample_dict("06-human-plaintext-secret.yaml")
    prompts: list[str] = []

    def recording(request: Any) -> dict[str, Any]:
        prompts.append(request.user)
        return oracle_review(request)

    retriever = CompositeRetriever(FileRetriever(), CaseRetriever(Source([case("rv_old.rejected", ["SEC-001"])])))
    state = initial_state(spec, review_id="rv_new", spec_ref={"repository": REPO, "commit": "c" * 40, "path": "deploy.yaml"})
    final = await run_graph(state, llm=ScriptedLLM(recording), retriever=retriever)

    assert "지난 검토 rv_old.rejected" in prompts[0]
    assert '"doc_type": "case"' in prompts[0]
    assert final["decision"]["validation"]["citations_ok"]


async def test_without_scope_attaches_no_case_and_skips_store() -> None:
    source = Source([case("rv_1.rejected", ["DB-001"])])

    assert await CaseRetriever(source).search([finding("DB-001")], "aws") == []
    assert source.calls == []


def test_scope_of_needs_app_and_repository() -> None:
    spec = load_sample_dict("06-human-plaintext-secret.yaml")
    ref = {"repository": REPO, "commit": "c" * 40, "path": "deploy.yaml"}

    assert scope_of(initial_state(spec, review_id="rv", spec_ref=ref)) == Scope(spec["metadata"]["name"], REPO)
    assert scope_of(initial_state(spec, review_id="rv")) is None  # 평가셋·로컬 실행 — 레포를 모른다


def test_approval_outcomes_are_untrusted_unless_flag_set() -> None:
    default = trusted_outcomes({})

    assert set(default) == set(OUTCOME_TEXT) - set(APPROVAL_OUTCOMES)
    assert {"rejected", "auto_fixed", "deploy_degraded"} <= set(default)
    assert trusted_outcomes({TRUST_APPROVALS_ENV: "1"}) == tuple(OUTCOME_TEXT)
    assert trusted_outcomes({TRUST_APPROVALS_ENV: "no"}) == default


async def test_default_retriever_drops_approval_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TRUST_APPROVALS_ENV, raising=False)
    source = Source([case("rv_1.human_approved", ["DB-001"]), case("rv_2.rejected", ["DB-001"])])

    docs = await CaseRetriever(source).search([finding("DB-001")], "aws", SCOPE)

    assert [d["chunk_id"] for d in docs] == ["case:rv_2.rejected"]
    assert "human_approved" not in source.calls[0]["outcomes"]

    monkeypatch.setenv(TRUST_APPROVALS_ENV, "true")
    docs = await CaseRetriever(source).search([finding("DB-001")], "aws", SCOPE)
    assert [d["chunk_id"] for d in docs] == ["case:rv_1.human_approved", "case:rv_2.rejected"]
