"""knowledge/ 원본 · S3 사본 · Qdrant 색인이 같은지 대조한다. 하나라도 어긋나면 종료 코드 1.

    .venv/bin/python scripts/check_knowledge_sync.py --bucket oneaction-review-docs-<계정ID> --qdrant-url http://localhost:6333
    .venv/bin/python scripts/check_knowledge_sync.py --qdrant-url http://localhost:6333     # S3 는 건너뛴다

비교: 로컬 문서 MD5 ↔ S3 ETag(문서 수·내용), 로컬 청크 ↔ Qdrant point(청크 수·본문).
S3 는 aws CLI 로 읽는다(sync_knowledge_s3.sh 와 같은 자격 증명). 어긋나면 고치는 명령을 함께 보여 준다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review_ai.retrieval.knowledge import KNOWLEDGE_DIR, load_chunks  # noqa: E402
from review_ai.retrieval.sync_check import (  # noqa: E402
    Diff,
    SyncReport,
    compare,
    expected_points,
    local_documents,
    s3_documents,
)


def list_bucket(bucket: str) -> list[dict]:
    out = subprocess.run(
        ["aws", "s3api", "list-objects-v2", "--bucket", bucket, "--output", "json"],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out or "{}").get("Contents", [])  # 빈 버킷이면 Contents 가 없다


async def qdrant_diff(url: str, chunks) -> Diff:
    from qdrant_client import AsyncQdrantClient

    from review_ai.retrieval.qdrant_retriever import COLLECTION, indexed_points, point_id

    actual = await indexed_points(AsyncQdrantClient(url=url))
    return compare(f"Qdrant {COLLECTION} 청크", expected_points(chunks, point_id), actual)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", default=None, help="S3 review-docs 버킷 (없으면 S3 비교를 건너뛴다)")
    parser.add_argument("--qdrant-url", default=None, help="Qdrant 주소 (없으면 Qdrant 비교를 건너뛴다)")
    parser.add_argument("--knowledge-dir", type=Path, default=KNOWLEDGE_DIR)
    args = parser.parse_args()
    if not (args.bucket or args.qdrant_url):
        parser.error("--bucket 과 --qdrant-url 중 하나는 있어야 한다")
    root = args.knowledge_dir.resolve()
    docs = local_documents(root)
    chunks = load_chunks(root)
    print(f"로컬 {root}: 문서 {len(docs)}개 · 청크 {len(chunks)}개")

    diffs = []
    if args.bucket:
        try:
            objects = list_bucket(args.bucket)
        except subprocess.CalledProcessError as exc:
            sys.exit(f"S3 목록을 못 읽었다 (자격 증명·버킷 이름 확인): {exc.stderr.strip()}")
        diffs.append(compare(f"S3 {args.bucket} 문서", docs, s3_documents(objects)))
    if args.qdrant_url:
        diffs.append(await qdrant_diff(args.qdrant_url, chunks))

    report = SyncReport(diffs)
    for diff in report.diffs:
        print("\n".join(diff.lines()))
    if report.ok:
        print("일치")
        return
    print("\n고치려면:")
    if args.bucket and not report.diffs[0].ok:
        print(f"  scripts/sync_knowledge_s3.sh {args.bucket} --apply")
    if args.qdrant_url and not report.diffs[-1].ok:
        print(f"  .venv/bin/python scripts/index_knowledge.py --qdrant-url {args.qdrant_url}   # 남은 청크도 지운다")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
