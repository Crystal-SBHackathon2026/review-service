"""판단 사례 검색 — 업무 DB review_cases 에서 finding 의 ruleId 로 지난 검토를 찾아 근거 문서로 붙인다.

- rule_id 정확 매칭, 같은 대상 환경 먼저, 최근 MAX_CASES_PER_RULE 개 (정렬은 저장소가 한다)
- chunk_id 는 case:<case_id>, rule_id 는 걸린 규칙 — 인용 검증(cited_rule_ids ⊆ 문서 rule_id)을 그대로 통과한다
- 사례 저장소가 실패해도 검토는 계속한다. 규칙 문서(FileRetriever)만으로 판단할 수 있다
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from review_ai.retrieval import Retriever
from review_ai.state import Doc, Finding

MAX_CASES_PER_RULE = 3
log = logging.getLogger(__name__)


class CaseSource(Protocol):
    async def find_cases(self, rule_id: str, *, target_env: str, limit: int) -> list[dict[str, Any]]: ...


def case_doc(case: dict[str, Any], rule_id: str) -> Doc:
    return Doc(chunk_id=f"case:{case['case_id']}", rule_id=rule_id, doc_type="case",
               provider=case["target_env"], score=1.0, match="exact_rule",
               source_uri=f"review://{case['review_id']}", text=case["summary"])


class CaseRetriever:
    def __init__(self, source: CaseSource, *, per_rule: int = MAX_CASES_PER_RULE) -> None:
        self._source = source
        self._per_rule = per_rule

    async def search(self, findings: Sequence[Finding], target_env: str) -> list[Doc]:
        docs: list[Doc] = []
        seen: set[str] = set()
        for rule_id in sorted({f["rule_id"] for f in findings}):
            try:
                cases = await self._source.find_cases(rule_id, target_env=target_env, limit=self._per_rule)
            except Exception:  # 사례는 보조 근거다 — 저장소 오류로 검토를 멈추지 않는다
                log.warning("사례 검색 실패 (%s) — 규칙 문서만으로 검토한다", rule_id, exc_info=True)
                continue
            for case in cases:
                doc = case_doc(case, rule_id)
                if doc["chunk_id"] not in seen:
                    seen.add(doc["chunk_id"])
                    docs.append(doc)
        return docs


class CompositeRetriever:
    """여러 검색기 결과를 순서대로 잇는다 (앞 검색기가 먼저). 같은 chunk_id 는 처음 것만."""

    def __init__(self, *retrievers: Retriever) -> None:
        self._retrievers = retrievers

    async def search(self, findings: Sequence[Finding], target_env: str) -> list[Doc]:
        docs: list[Doc] = []
        seen: set[str] = set()
        for retriever in self._retrievers:
            for doc in await retriever.search(findings, target_env):
                if doc["chunk_id"] not in seen:
                    seen.add(doc["chunk_id"])
                    docs.append(doc)
        return docs
