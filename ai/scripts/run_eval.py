"""환각 평가셋 실행.

    .venv/bin/python scripts/run_eval.py                       # 가짜 LLM (키 없이)
    ANTHROPIC_API_KEY=... .venv/bin/python scripts/run_eval.py --llm claude --repeat 3
    .venv/bin/python scripts/run_eval.py --retriever qdrant --qdrant-url http://localhost:6333

결과 JSON 은 eval/reports/ 에 남는다 (git 에는 올리지 않는다).
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

from review_ai.evaluation import REVIEWER, load_eval_cases, run_case, summarize  # noqa: E402
from review_ai.judge.fake_llm import FAKES  # noqa: E402
from review_ai.judge.llm import CachedLLM, ClaudeLLM  # noqa: E402
from review_ai.retrieval.file_retriever import FileRetriever  # noqa: E402


async def make_retriever(kind: str, url: str | None):
    if kind == "file":
        return FileRetriever()
    from qdrant_client import AsyncQdrantClient

    from review_ai.retrieval.embedding import FastEmbedder
    from review_ai.retrieval.knowledge import load_chunks
    from review_ai.retrieval.qdrant_retriever import QdrantRetriever, index_chunks

    client = AsyncQdrantClient(url=url) if url else AsyncQdrantClient(location=":memory:")
    embedder = FastEmbedder()
    if not url:
        await index_chunks(client, embedder, load_chunks())
    return QdrantRetriever(client, embedder)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", choices=["fake", "claude"], default="fake")
    parser.add_argument("--model", default=None, help="claude 모델 ID (기본 REVIEW_LLM_MODEL 또는 claude-sonnet-5-5)")
    parser.add_argument("--repeat", type=int, default=1, help="reviewer 케이스 반복 횟수")
    parser.add_argument("--retriever", choices=["file", "qdrant"], default="file")
    parser.add_argument("--qdrant-url", default=None)
    parser.add_argument("--only", default=None, help="케이스 id 접두사")
    args = parser.parse_args()

    if args.llm == "claude":
        claude = ClaudeLLM(**({"model": args.model} if args.model else {}))
        reviewer = lambda: claude  # noqa: E731 — 반복마다 새로 호출해야 흔들림을 잰다 (캐시 없음)
    else:
        reviewer = FAKES["oracle"]
    retriever = await make_retriever(args.retriever, args.qdrant_url)

    results = []
    for case in load_eval_cases():
        if args.only and not case["id"].startswith(args.only):
            continue
        times = args.repeat if case["llm"] == REVIEWER else 1
        for _ in range(times):
            result = await run_case(case, reviewer, retriever)
            results.append(result)
            mark = "OK " if result.ok else "FAIL"
            print(f"{mark} {case['id']:<40} {result.verdict:<12} {','.join(result.reasons) or '-':<30} "
                  f"llm={result.llm_calls} {result.seconds:.1f}s {'; '.join(result.failures)}")

    summary = summarize(results)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    out_dir = ROOT / "eval" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{args.llm}.json"
    out.write_text(json.dumps({"args": vars(args), "summary": summary, "results": [asdict(r) for r in results]},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {out.relative_to(ROOT)}")
    if summary["match_rate"] < 1.0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
