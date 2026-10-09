"""retrieve_evidence — finding 의 ruleId 로 규칙 문서를 정확히 찾고, 사례·가이드는 의미 검색으로 보탠다.

임계값은 semantic 점수(dense cosine)에만 쓴다. ruleId 정확 매칭은 점수 1.0 고정이고 LOW_SCORE 대상이 아니다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, NamedTuple, Protocol

from review_ai.retrieval.knowledge import Chunk
from review_ai.state import Doc, Finding

MAX_EXACT_PER_RULE = 8

Node = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class Scope(NamedTuple):
    """검토 대상 — 판단 사례(review_cases)를 같은 앱·레포의 지난 검토로 한정할 때 쓴다. 규칙 문서 검색은 쓰지 않는다."""
    app: str
    repository: str


class Retriever(Protocol):
    async def search(self, findings: Sequence[Finding], target_env: str, scope: Scope | None = None) -> list[Doc]: ...


def scope_of(state: dict[str, Any]) -> Scope | None:
    """State → Scope. 레포를 모르면(평가셋·로컬 실행) None — 사례 검색기는 아무 사례도 붙이지 않는다."""
    app = ((state.get("deploy_spec") or {}).get("metadata") or {}).get("name")
    repository = (state.get("spec_ref") or {}).get("repository")
    return Scope(app=app, repository=repository) if app and repository else None


def to_doc(chunk: Chunk, *, rule_id: str | None, score: float, match: str) -> Doc:
    return Doc(
        chunk_id=chunk.chunk_id,
        rule_id=rule_id,
        doc_type=chunk.doc_type,  # type: ignore[typeddict-item]
        provider=chunk.provider,  # type: ignore[typeddict-item]
        score=score,
        match=match,  # type: ignore[typeddict-item]
        source_uri=chunk.source_uri,
        text=chunk.text,
    )


def exact_first(chunks: Sequence[Chunk]) -> list[Chunk]:
    """ruleId 정확 매칭 결과에서 MAX_EXACT_PER_RULE 개를 고른다 — 규칙 문서(정본)를 먼저, 사례·가이드는 그 뒤에.

    경로 순서로 자르면 guides/·incidents/ 가 rules/ 보다 앞서서, 관련 사례가 많은 규칙(DB-003)은 규칙 문서가 통째로 빠졌다.
    같은 문서 안에서는 섹션 순서(chunk_id 의 #번호)를 지킨다.
    """
    def key(c: Chunk) -> tuple[bool, str, int]:
        return c.doc_type != "rule", c.source_uri, int(c.chunk_id.rsplit("#", 1)[1])

    return sorted(chunks, key=key)[:MAX_EXACT_PER_RULE]


def provider_ok(chunk_provider: str, target_env: str) -> bool:
    return chunk_provider in ("any", target_env)


def dedupe(docs: Sequence[Doc]) -> list[Doc]:
    """같은 청크가 여러 finding 에 걸리면 한 번만. exact 가 semantic 보다 앞선다."""
    seen: set[str] = set()
    out = []
    for doc in sorted(docs, key=lambda d: (d["match"] != "exact_rule", -d["score"], d["chunk_id"])):
        if doc["chunk_id"] not in seen:
            seen.add(doc["chunk_id"])
            out.append(doc)
    return out


def make_retrieve_evidence(retriever: Retriever) -> Node:
    async def retrieve_evidence(state: dict[str, Any]) -> dict[str, Any]:
        findings = state.get("findings") or []
        if not findings:
            return {"retrieved_docs": []}
        return {"retrieved_docs": await retriever.search(findings, state["target_env"], scope_of(state))}

    return retrieve_evidence
