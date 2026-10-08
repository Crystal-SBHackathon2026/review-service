"""Qdrant 검색기 — ruleId 는 payload 필터로 정확히, 사례·가이드는 dense cosine 으로.

컬렉션 하나(review-knowledge)에 모든 청크를 넣고 payload 로 구분한다:
  doc_type, provider, rule_ids, warning_code, title, text, source_uri
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from review_ai.errors import TransientError
from review_ai.retrieval import dedupe, exact_first, to_doc
from review_ai.retrieval.embedding import Embedder
from review_ai.retrieval.knowledge import Chunk
from review_ai.state import Doc, Finding

COLLECTION = "review-knowledge"
SEMANTIC_LIMIT = 3
# 이보다 낮은 의미 검색 결과는 프롬프트에 넣지 않는다. 다국어 MiniLM 으로 이 문서들에 재 보니 무관한 사례가 0.34~0.43 이었다
SEMANTIC_MIN_SCORE = 0.5
SEMANTIC_DOC_TYPES = ["incident", "guide"]
SCROLL_PAGE = 256


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))  # 다시 색인해도 같은 ID — 멱등


def _payload_chunk(payload: dict) -> Chunk:
    return Chunk(
        chunk_id=payload["chunk_id"], doc_type=payload["doc_type"], provider=payload["provider"],
        rule_ids=tuple(payload["rule_ids"]), title=payload["title"], text=payload["text"],
        source_uri=payload["source_uri"], warning_code=payload.get("warning_code"),
    )


def _provider_filter(target_env: str) -> models.FieldCondition:
    return models.FieldCondition(key="provider", match=models.MatchAny(any=["any", target_env]))


async def index_chunks(client: AsyncQdrantClient, embedder: Embedder, chunks: Sequence[Chunk]) -> int:
    if not await client.collection_exists(COLLECTION):
        await client.create_collection(
            COLLECTION, vectors_config=models.VectorParams(size=embedder.dim, distance=models.Distance.COSINE)
        )
        for key in ("doc_type", "provider", "rule_ids"):
            await client.create_payload_index(COLLECTION, key, models.PayloadSchemaType.KEYWORD)
    vectors = await embedder.embed([c.text for c in chunks])
    points = [
        models.PointStruct(id=point_id(c.chunk_id), vector=v, payload={
            "chunk_id": c.chunk_id, "doc_type": c.doc_type, "provider": c.provider,
            "rule_ids": list(c.rule_ids), "warning_code": c.warning_code, "title": c.title, "text": c.text,
            "source_uri": c.source_uri,
        })
        for c, v in zip(chunks, vectors, strict=True)
    ]
    await client.upsert(COLLECTION, points=points)
    return len(points)


async def indexed_points(client: AsyncQdrantClient) -> dict[str, str]:
    """컬렉션의 모든 point → {ID: 본문}. 컬렉션이 없으면 빈 dict."""
    if not await client.collection_exists(COLLECTION):
        return {}
    out: dict[str, str] = {}
    offset = None
    while True:
        points, offset = await client.scroll(COLLECTION, limit=SCROLL_PAGE, offset=offset, with_payload=["text"])
        out.update({str(p.id): (p.payload or {}).get("text", "") for p in points})
        if offset is None:
            return out


async def prune_points(client: AsyncQdrantClient, keep: set[str]) -> list[str]:
    """keep 에 없는 point 를 지운다 — 지운 문서·줄어든 섹션이 색인에 남아 검색되지 않게. 지운 ID 를 돌려준다."""
    stale = sorted(set(await indexed_points(client)) - keep)
    if stale:
        await client.delete(COLLECTION, points_selector=models.PointIdsList(points=stale))
    return stale


class QdrantRetriever:
    def __init__(self, client: AsyncQdrantClient, embedder: Embedder, *, min_score: float = SEMANTIC_MIN_SCORE) -> None:
        self._client = client
        self._embedder = embedder
        self._min_score = min_score

    async def _exact(self, rule_id: str, target_env: str) -> list[Doc]:
        flt = models.Filter(must=[
            models.FieldCondition(key="rule_ids", match=models.MatchValue(value=rule_id)),
            _provider_filter(target_env),
        ])
        # scroll 은 point ID 순이라 앞에서 자르면 규칙 문서가 빠질 수 있다 — 한 페이지를 다 받아 exact_first 로 고른다
        points, _ = await self._client.scroll(COLLECTION, scroll_filter=flt, limit=SCROLL_PAGE)
        chunks = exact_first([_payload_chunk(p.payload or {}) for p in points])
        return [to_doc(c, rule_id=rule_id, score=1.0, match="exact_rule") for c in chunks]

    async def _semantic(self, vector: list[float], target_env: str, *, limit: int = SEMANTIC_LIMIT,
                        min_score: float | None = None) -> list[Doc]:
        flt = models.Filter(must=[
            models.FieldCondition(key="doc_type", match=models.MatchAny(any=SEMANTIC_DOC_TYPES)),
            _provider_filter(target_env),
        ])
        result = await self._client.query_points(COLLECTION, query=vector, query_filter=flt, limit=limit,
                                                 score_threshold=min_score)
        return [
            to_doc(_payload_chunk(p.payload or {}), rule_id=None, score=float(p.score), match="semantic")
            for p in result.points
        ]

    async def search(self, findings: Sequence[Finding], target_env: str) -> list[Doc]:
        try:
            vectors = await self._embedder.embed([f"{f['title']} — {f['evidence']}" for f in findings])
            batches = await asyncio.gather(
                *(self._exact(rule_id, target_env) for rule_id in sorted({f["rule_id"] for f in findings})),
                *(self._semantic(vector, target_env, min_score=self._min_score) for vector in vectors),
            )
            docs = [doc for batch in batches for doc in batch]
        except (ConnectionError, TimeoutError, ResponseHandlingException) as exc:
            raise TransientError(f"Qdrant 연결 실패: {exc}") from exc
        except UnexpectedResponse as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                raise TransientError(f"Qdrant 일시 오류 {exc.status_code}") from exc
            raise
        return dedupe(docs)

    async def rank(self, query: str, target_env: str, *, limit: int = SEMANTIC_LIMIT) -> list[Doc]:
        """검색 평가용 — search 의 의미 검색과 같은 필터로, 임계값 없이 점수 순 상위 limit 청크."""
        [vector] = await self._embedder.embed([query])
        return await self._semantic(vector, target_env, limit=limit)
