"""retrieve_evidence — finding 의 ruleId 로 규칙 문서를 정확히 찾고, 사례·가이드는 의미 검색으로 보탠다.

임계값은 semantic 점수(dense cosine)에만 쓴다. ruleId 정확 매칭은 점수 1.0 고정이고 LOW_SCORE 대상이 아니다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from review_ai.retrieval.knowledge import Chunk
from review_ai.state import Doc, Finding

MAX_EXACT_PER_RULE = 8

Node = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class Retriever(Protocol):
    async def search(self, findings: Sequence[Finding], target_env: str) -> list[Doc]: ...


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
        return {"retrieved_docs": await retriever.search(findings, state["target_env"])}

    return retrieve_evidence
