"""명세 생성·검토 미리보기. Git 쓰기·Kafka 발행은 하지 않는다.

python scripts/prepare_spec.py --context samples/generation-context-sample-app-aws.yaml
--spec deploy.yaml 을 생략하면 명세 미전달을, 빈 파일이면 빈 명세를 재현한다.
--claude 를 명시하면 배포와 같은 Claude 클라이언트를 쓴다(ANTHROPIC_API_KEY 필요).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import yaml

from review_ai.judge.llm import CachedLLM, ClaudeLLM
from review_ai.preparation import GenerationContext, prepare_and_review
from review_ai.retrieval.file_retriever import FileRetriever


async def main() -> None:
    parser = argparse.ArgumentParser(description="빈 배포 명세 생성 → 정적 검사·RAG 리뷰 → JSON 반환")
    parser.add_argument("--context", type=Path, required=True, help="파이프라인의 앱·대상 설정 YAML")
    parser.add_argument("--spec", type=Path, help="기존 명세. 생략하면 새로 생성")
    parser.add_argument("--claude", action="store_true", help="Claude 로 설명·허용된 자동 수정 수행")
    args = parser.parse_args()
    context = GenerationContext.model_validate(yaml.safe_load(args.context.read_text(encoding="utf-8")))
    raw = args.spec.read_text(encoding="utf-8") if args.spec else None
    result = await prepare_and_review(raw, context=context, review_id="prepare-preview",
                                     llm=CachedLLM(ClaudeLLM()) if args.claude else None,
                                     retriever=FileRetriever())
    print(json.dumps(result.to_response(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
