"""임베딩. 기본은 로컬 fastembed 다국어 모델 — API 키 없이 돌고, 문서가 한국어라 영어 전용 모델은 맞지 않는다."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# 한 번에 임베딩할 문서 수. fastembed 기본(256)이면 170청크를 한 배치로 올려 색인 컨테이너가 1Gi 를 넘는다(OOM 실측)
EMBED_BATCH_SIZE = 16


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FastEmbedder:
    def __init__(self, model: str = DEFAULT_MODEL, *, batch_size: int = EMBED_BATCH_SIZE) -> None:
        from fastembed import TextEmbedding  # 무거운 import 는 실제로 쓸 때만

        self._model = TextEmbedding(model_name=model)
        self._batch_size = batch_size
        self.dim = len(next(iter(self._model.embed(["dim"]))))

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return await asyncio.to_thread(
            lambda: [v.tolist() for v in self._model.embed(list(texts), batch_size=self._batch_size)])


class HashEmbedder:
    """테스트용 결정적 임베딩 (단어 해시 bag-of-words). 의미를 알지 못한다."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in re.findall(r"\w+", text.lower()):
            vec[int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]
