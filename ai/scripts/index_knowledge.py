"""knowledge/ 를 Qdrant 에 색인한다. 같은 청크는 같은 point ID 라 다시 돌려도 중복되지 않는다.
knowledge/ 에 없는 point(지운 문서·줄어든 섹션)는 지운다 — 색인 = knowledge/ 가 되도록. 끄려면 --no-prune.

    docker compose up -d qdrant
    .venv/bin/python scripts/index_knowledge.py --qdrant-url http://localhost:6333

S3 review-docs 버킷을 받아 색인할 때는 내려받은 디렉터리를 --knowledge-dir 로 준다(버킷 경로 = knowledge/ 경로).

    aws s3 sync s3://<bucket>/ /tmp/knowledge && ... --knowledge-dir /tmp/knowledge
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qdrant_client import AsyncQdrantClient  # noqa: E402

from review_ai.retrieval.embedding import DEFAULT_MODEL, FastEmbedder  # noqa: E402
from review_ai.retrieval.knowledge import KNOWLEDGE_DIR, load_chunks  # noqa: E402
from review_ai.retrieval.qdrant_retriever import COLLECTION, index_chunks, point_id, prune_points  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qdrant-url", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--knowledge-dir", type=Path, default=KNOWLEDGE_DIR)
    parser.add_argument("--no-prune", action="store_true", help="knowledge/ 에 없는 point 를 남겨 둔다")
    args = parser.parse_args()
    if not args.knowledge_dir.is_dir():
        parser.error(f"--knowledge-dir 가 디렉터리가 아니다: {args.knowledge_dir}")
    chunks = load_chunks(args.knowledge_dir.resolve())
    if not chunks:
        parser.error(f"색인할 문서가 없다: {args.knowledge_dir}")
    client = AsyncQdrantClient(url=args.qdrant_url)
    count = await index_chunks(client, FastEmbedder(args.model), chunks)
    stale = [] if args.no_prune else await prune_points(client, {point_id(c.chunk_id) for c in chunks})
    per_env = {env: sum(1 for c in chunks if c.provider in ("any", env)) for env in ("aws", "gcp", "local")}
    print(f"{COLLECTION}: {count}개 청크 색인 · 남은 청크 {len(stale)}개 삭제 · "
          f"종류 {dict(Counter(c.doc_type for c in chunks))} · 환경별 {per_env}")


if __name__ == "__main__":
    asyncio.run(main())
