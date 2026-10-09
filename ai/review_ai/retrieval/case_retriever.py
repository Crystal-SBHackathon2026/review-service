"""판단 사례 검색 — 업무 DB review_cases 에서 finding 의 ruleId 로 지난 검토를 찾아 근거 문서로 붙인다.

- rule_id 정확 매칭, 같은 대상 환경 먼저, 최근 MAX_CASES_PER_RULE 개 (정렬은 저장소가 한다)
- 같은 앱·레포(Scope)의 지난 검토만 — 다른 앱의 승인은 이 배포의 근거가 아니고, 잘못 쌓인 사례가 번지는 범위도 그 앱으로 줄인다.
  Scope 를 모르면 사례를 붙이지 않는다
- 사람 승인으로 끝난 사례(APPROVAL_OUTCOMES)는 REVIEW_CASES_TRUST_APPROVALS=1 일 때만 근거로 쓴다. 승인 API 에 인증이
  없는 동안은 누구나 승인을 보낼 수 있고, 그 사례는 다음 judge 를 승인 쪽으로 기울이며 사람이 넣은 값까지 프롬프트에 싣는다.
  거절·AI 수정·배포 실패 사례는 judge 를 보수적으로만 움직여서 계속 쓴다. 저장은 종료 방식과 무관하게 그대로 한다
- chunk_id 는 case:<case_id>, rule_id 는 걸린 규칙 — 인용 검증(cited_rule_ids ⊆ 문서 rule_id)을 그대로 통과한다
- 사례 저장소가 실패해도 검토는 계속한다. 규칙 문서(FileRetriever)만으로 판단할 수 있다
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any, Protocol

from review_ai.cases import OUTCOME_TEXT
from review_ai.retrieval import Retriever, Scope
from review_ai.state import Doc, Finding

MAX_CASES_PER_RULE = 3
APPROVAL_OUTCOMES = ("human_approved", "human_edited", "recommended")
TRUST_APPROVALS_ENV = "REVIEW_CASES_TRUST_APPROVALS"
log = logging.getLogger(__name__)


class CaseSource(Protocol):
    async def find_cases(self, rule_id: str, *, app: str, repository: str, target_env: str,
                         outcomes: Sequence[str], limit: int) -> list[dict[str, Any]]: ...


def trusted_outcomes(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """근거로 쓸 종료 방식. 승인 API 인증이 생기기 전에는 사람 승인 사례를 뺀다 (모듈 docstring)."""
    env = os.environ if environ is None else environ
    if env.get(TRUST_APPROVALS_ENV, "").strip().lower() in ("1", "true", "yes"):
        return tuple(OUTCOME_TEXT)
    return tuple(o for o in OUTCOME_TEXT if o not in APPROVAL_OUTCOMES)


def case_doc(case: dict[str, Any], rule_id: str) -> Doc:
    return Doc(chunk_id=f"case:{case['case_id']}", rule_id=rule_id, doc_type="case",
               provider=case["target_env"], score=1.0, match="exact_rule",
               source_uri=f"review://{case['review_id']}", text=case["summary"])


class CaseRetriever:
    def __init__(self, source: CaseSource, *, per_rule: int = MAX_CASES_PER_RULE,
                 outcomes: Sequence[str] | None = None) -> None:
        self._source = source
        self._per_rule = per_rule
        self._outcomes = tuple(outcomes) if outcomes is not None else trusted_outcomes()

    async def search(self, findings: Sequence[Finding], target_env: str, scope: Scope | None = None) -> list[Doc]:
        if scope is None:
            return []
        docs: list[Doc] = []
        seen: set[str] = set()
        for rule_id in sorted({f["rule_id"] for f in findings}):
            try:
                cases = await self._source.find_cases(rule_id, app=scope.app, repository=scope.repository,
                                                      target_env=target_env, outcomes=self._outcomes,
                                                      limit=self._per_rule)
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

    async def search(self, findings: Sequence[Finding], target_env: str, scope: Scope | None = None) -> list[Doc]:
        docs: list[Doc] = []
        seen: set[str] = set()
        for retriever in self._retrievers:
            for doc in await retriever.search(findings, target_env, scope):
                if doc["chunk_id"] not in seen:
                    seen.add(doc["chunk_id"])
                    docs.append(doc)
        return docs
