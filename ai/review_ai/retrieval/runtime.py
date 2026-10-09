"""운영 검색기 조립 — 규칙 문서 파일 검색은 항상, Qdrant 의미 검색은 QDRANT_URL 이 있을 때만 덧붙인다.

클러스터의 Qdrant 는 디스크 없이(emptyDir) 떠서, 파드가 다시 뜨면 색인 컨테이너가 채울 때까지 비어 있다.
그동안이나 연결이 끊겼을 때도 검토는 파일 검색만으로 지금처럼 돈다 — Qdrant 오류는 경고만 남기고 빈 결과로 본다.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from review_ai.retrieval import Retriever
from review_ai.retrieval.case_retriever import CompositeRetriever
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.state import Doc, Finding

QDRANT_TIMEOUT_SECONDS = 5
log = logging.getLogger(__name__)


class OptionalRetriever:
    """보조 검색기 — 실패하면 경고만 남기고 빈 결과. 규칙 문서는 앞의 FileRetriever 가 이미 붙였다."""

    def __init__(self, retriever: Retriever, name: str) -> None:
        self._retriever = retriever
        self._name = name

    async def search(self, findings: Sequence[Finding], target_env: str, *args: Any, **kwargs: Any) -> list[Doc]:
        # 뒤 인자(검색 범위 scope 등)는 감싼 검색기에 그대로 넘긴다 — Retriever 프로토콜에 인자가 늘어도 깨지지 않게
        try:
            return await self._retriever.search(findings, target_env, *args, **kwargs)
        except Exception:
            log.warning("%s 검색 실패 — 이번 검토는 %s 없이 진행한다", self._name, self._name, exc_info=True)
            return []


def qdrant_from_url(url: str | None) -> Retriever | None:
    """URL 이 없거나 검색기를 만들지 못하면(패키지·임베딩 모델 없음) None — 워커는 파일 검색만으로 뜬다."""
    if not url:
        return None
    try:
        from qdrant_client import AsyncQdrantClient

        from review_ai.retrieval.embedding import FastEmbedder
        from review_ai.retrieval.qdrant_retriever import QdrantRetriever

        client = AsyncQdrantClient(url=url, timeout=QDRANT_TIMEOUT_SECONDS, check_compatibility=False)
        return QdrantRetriever(client, FastEmbedder())
    except Exception:
        log.warning("Qdrant 검색기를 만들지 못했다 (%s) — 파일 검색만 쓴다", url, exc_info=True)
        return None


def make_retriever(*extra: Retriever, qdrant: Retriever | None = None) -> Retriever:
    """파일 검색 → Qdrant 의미 검색 → extra(판단 사례 등) 순서. 같은 chunk_id 는 앞 검색기 것만 남는다.

    파일 검색이 맨 앞이라 규칙 문서(exact_rule)는 Qdrant 가 비어 있어도 항상 붙고,
    Qdrant 는 그 뒤에 사례·가이드 의미 검색(semantic) 결과만 보탠다.
    """
    retrievers: list[Retriever] = [FileRetriever()]
    if qdrant is not None:
        retrievers.append(OptionalRetriever(qdrant, "Qdrant"))
    return CompositeRetriever(*retrievers, *extra)
