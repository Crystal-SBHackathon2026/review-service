"""검색 평가셋 실행 — 질문마다 상위 3개 청크 안에 정답 문서가 있는지 잰다.

    .venv/bin/python scripts/eval_retrieval.py                                   # 로컬 임베딩 + 메모리 Qdrant
    .venv/bin/python scripts/eval_retrieval.py --qdrant-url http://localhost:6333  # 이미 색인한 서버

결과 JSON 은 eval/reports/ 에 남는다 (git 에는 올리지 않는다).
정답이 상위 3개에 없고 ruleId 정확 매칭으로도 보완되지 않는 질문이 있으면 종료 코드 1.
RULE = 의미 검색은 놓쳤지만 정답 문서가 ruleId 정확 매칭으로 이미 들어간다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qdrant_client import AsyncQdrantClient  # noqa: E402

from review_ai.retrieval.embedding import DEFAULT_MODEL, FastEmbedder  # noqa: E402
from review_ai.retrieval.evaluation import load_retrieval_cases, run_retrieval_case, summarize_retrieval  # noqa: E402
from review_ai.retrieval.knowledge import load_chunks  # noqa: E402
from review_ai.retrieval.qdrant_retriever import SEMANTIC_MIN_SCORE, QdrantRetriever, index_chunks  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qdrant-url", default=None, help="없으면 메모리 Qdrant 에 knowledge/ 를 색인해서 잰다")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--min-score", type=float, default=SEMANTIC_MIN_SCORE, help="워커 임계값과 같게 둔다")
    args = parser.parse_args()

    chunks = load_chunks()
    embedder = FastEmbedder(args.model)
    client = AsyncQdrantClient(url=args.qdrant_url) if args.qdrant_url else AsyncQdrantClient(location=":memory:")
    if not args.qdrant_url:
        await index_chunks(client, embedder, chunks)
    retriever = QdrantRetriever(client, embedder, min_score=args.min_score)

    results = []
    for case in load_retrieval_cases():
        r = await run_retrieval_case(case, retriever, min_score=args.min_score, chunks=chunks)
        results.append(r)
        top = " ".join(f"{uri.split('/')[-1][:24]}:{score:.2f}" for uri, score in r.top)
        best = f"{r.best_score:.2f}" if r.best_score is not None else "-"
        mark = "OK  " if r.ok else ("RULE" if r.covered_by_rule else "MISS")
        print(f"{mark} {r.case_id:<4} {r.kind:<8} rank={r.rank or '-':<3} best={best:<5} {top}")

    summary = summarize_retrieval(results)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    out_dir = ROOT / "eval" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}-retrieval.json"
    out.write_text(json.dumps({"args": vars(args), "summary": summary, "results": [asdict(r) for r in results]},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {out.relative_to(ROOT)}")
    if summary["misses"]:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
