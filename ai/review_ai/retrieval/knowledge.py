"""knowledge/ 아래 마크다운을 청크로 읽는다. S3 review-docs 버킷의 경로 구조(rules/{env}/·incidents/·guides/·warnings/)와 같다.

청크 = 문서의 '## ' 섹션 하나. 각 청크 앞에 문서 제목을 붙여 단독으로 읽혀도 맥락이 남게 한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from review_ai.resources import data_dir

KNOWLEDGE_DIR = data_dir("knowledge")
FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_type: str  # rule · incident · guide · warning
    provider: str  # aws · gcp · local · any
    rule_ids: tuple[str, ...]  # rule 문서는 자기 ID 하나, incident·guide 는 related_rules, warning 은 비어 있다
    title: str
    text: str
    source_uri: str
    # warning 문서만 — 렌더러 경고 code. 규칙이 아니라서 rule_ids 에 넣지 않는다(judge 프롬프트에 섞이지 않게)
    warning_code: str | None = None


def _sections(body: str) -> list[str]:
    parts = re.split(r"(?m)^## ", body)
    return [f"## {p.strip()}" for p in parts if p.strip()]


def parse_document(text: str, source_uri: str) -> list[Chunk]:
    match = FRONT_MATTER.match(text)
    if match is None:
        raise ValueError(f"front matter 없음: {source_uri}")
    meta, body = yaml.safe_load(match.group(1)), match.group(2)
    rule_ids = (meta["rule_id"],) if meta.get("rule_id") else tuple(meta.get("related_rules") or ())
    return [
        Chunk(
            chunk_id=f"{source_uri}#{i}",
            doc_type=meta["doc_type"],
            provider=meta["provider"],
            rule_ids=rule_ids,
            title=meta["title"],
            text=f"# {meta['title']}\n{section}",
            source_uri=source_uri,
            warning_code=meta.get("warning_code"),
        )
        for i, section in enumerate(_sections(body))
    ]


@lru_cache(maxsize=4)
def load_chunks(root: Path = KNOWLEDGE_DIR) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*.md")):
        chunks.extend(parse_document(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix()))
    return tuple(chunks)
