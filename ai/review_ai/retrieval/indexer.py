"""knowledge/ 를 Qdrant 에 색인한다 — python -m review_ai.retrieval.indexer --qdrant-url http://qdrant:6333

같은 청크는 같은 point ID 라 다시 돌려도 중복되지 않는다. knowledge/ 에 없는 point(지운 문서·줄어든 섹션)는
지운다 — 색인 = knowledge/ 가 되도록. 끄려면 --no-prune.

기본 문서는 이 패키지에 들어 있는 knowledge/ 다(이미지 빌드 때 review_ai/_data/knowledge 로 복사). 그래서 워커와
같은 이미지로 돌리면 워커 코드와 같은 버전의 문서가 색인된다. 클러스터에서는 Qdrant 파드의 색인 컨테이너가
파드가 뜰 때마다 이걸 돌린다(--wait 로 Qdrant 가 뜰 때까지 기다린다).
S3 review-docs 버킷으로 색인할 때는 내려받은 디렉터리를 --knowledge-dir 로 준다(버킷 경로 = knowledge/ 경로).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from qdrant_client import AsyncQdrantClient

from review_ai.retrieval.embedding import DEFAULT_MODEL, Embedder
from review_ai.retrieval.knowledge import KNOWLEDGE_DIR, Chunk, load_chunks
from review_ai.retrieval.qdrant_retriever import COLLECTION, index_chunks, point_id, prune_points

WAIT_INTERVAL_SECONDS = 2.0
log = logging.getLogger(__name__)


async def wait_ready(client: AsyncQdrantClient, timeout: float, *, interval: float = WAIT_INTERVAL_SECONDS) -> None:
    """Qdrant 가 응답할 때까지 기다린다. timeout 초가 지나도 안 되면 마지막 오류를 그대로 올린다."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            await client.get_collections()
            return
        except Exception:
            if loop.time() >= deadline:
                raise
            log.info("Qdrant 응답 대기 중")
            await asyncio.sleep(interval)


async def index(client: AsyncQdrantClient, embedder: Embedder, chunks: Sequence[Chunk], *, prune: bool = True) -> str:
    """색인하고 한 줄 요약을 돌려준다."""
    count = await index_chunks(client, embedder, chunks)
    stale = await prune_points(client, {point_id(c.chunk_id) for c in chunks}) if prune else []
    per_env = {env: sum(1 for c in chunks if c.provider in ("any", env)) for env in ("aws", "gcp", "local")}
    return (f"{COLLECTION}: {count}개 청크 색인 · 남은 청크 {len(stale)}개 삭제 · "
            f"종류 {dict(Counter(c.doc_type for c in chunks))} · 환경별 {per_env}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m review_ai.retrieval.indexer")
    parser.add_argument("--qdrant-url", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--knowledge-dir", type=Path, default=KNOWLEDGE_DIR)
    parser.add_argument("--no-prune", action="store_true", help="knowledge/ 에 없는 point 를 남겨 둔다")
    parser.add_argument("--wait", type=float, default=0.0, help="Qdrant 가 뜰 때까지 최대 몇 초 기다릴지")
    args = parser.parse_args(argv)
    if not args.knowledge_dir.is_dir():
        parser.error(f"--knowledge-dir 가 디렉터리가 아니다: {args.knowledge_dir}")
    return args


async def run(argv: Sequence[str] | None = None) -> str:
    args = parse_args(argv)
    chunks = load_chunks(args.knowledge_dir.resolve())
    if not chunks:
        raise SystemExit(f"색인할 문서가 없다: {args.knowledge_dir}")
    client = AsyncQdrantClient(url=args.qdrant_url, check_compatibility=False)  # 버전 확인은 wait_ready 뒤 첫 호출에서
    await wait_ready(client, args.wait)
    from review_ai.retrieval.embedding import FastEmbedder  # 무거운 import(onnxruntime) 는 Qdrant 가 뜬 뒤에

    return await index(client, FastEmbedder(args.model), chunks, prune=not args.no_prune)


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    print(asyncio.run(run(argv)), flush=True)


if __name__ == "__main__":
    main()
