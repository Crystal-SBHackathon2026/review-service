"""벡터 DB 없이 ruleId 로만 찾는 검색기. Qdrant 가 없을 때·테스트·평가셋에서 쓴다."""

from __future__ import annotations

from collections.abc import Sequence

from review_ai.retrieval import MAX_EXACT_PER_RULE, dedupe, provider_ok, to_doc
from review_ai.retrieval.knowledge import Chunk, load_chunks
from review_ai.state import Doc, Finding


class FileRetriever:
    def __init__(self, chunks: Sequence[Chunk] | None = None) -> None:
        self._chunks = tuple(chunks) if chunks is not None else load_chunks()

    async def search(self, findings: Sequence[Finding], target_env: str) -> list[Doc]:
        docs: list[Doc] = []
        for rule_id in sorted({f["rule_id"] for f in findings}):
            hits = [c for c in self._chunks if rule_id in c.rule_ids and provider_ok(c.provider, target_env)]
            docs.extend(to_doc(c, rule_id=rule_id, score=1.0, match="exact_rule") for c in hits[:MAX_EXACT_PER_RULE])
        return dedupe(docs)
