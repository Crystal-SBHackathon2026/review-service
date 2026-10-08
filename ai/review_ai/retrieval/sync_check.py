"""knowledge/ 원본 · S3 사본 · Qdrant 색인이 같은지 대조한다.

개수만 보면 '하나 고치고 하나 지운' 경우를 놓친다. 그래서 문서는 내용 해시(S3 ETag = MD5, SSE-S3·단일 파트 업로드)로,
청크는 point ID 와 본문으로 비교한다. 어긋난 항목 이름을 돌려주므로 무엇을 다시 올리거나 색인할지 바로 보인다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from review_ai.retrieval.knowledge import Chunk


@dataclass(frozen=True)
class Diff:
    """한쪽(기준)과 다른 쪽(대상)의 차이. 비어 있으면 같다."""

    name: str
    expected: int
    actual: int
    missing: tuple[str, ...] = ()  # 기준에 있는데 대상에 없다 → 올리기·색인 누락
    extra: tuple[str, ...] = ()  # 대상에만 있다 → 지운 문서·사라진 섹션이 남아 있다
    changed: tuple[str, ...] = ()  # 둘 다 있는데 내용이 다르다

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.changed)

    def lines(self) -> list[str]:
        head = f"{'OK  ' if self.ok else 'DIFF'} {self.name}: 기준 {self.expected} · 대상 {self.actual}"
        out = [head]
        for label, items in (("없음", self.missing), ("남음", self.extra), ("다름", self.changed)):
            out.extend(f"       {label} {item}" for item in items)
        return out


@dataclass(frozen=True)
class SyncReport:
    diffs: list[Diff] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(d.ok for d in self.diffs)


def compare(name: str, expected: Mapping[str, str], actual: Mapping[str, str]) -> Diff:
    """키 → 내용 지문(해시·본문) 두 벌을 비교한다."""
    return Diff(
        name=name,
        expected=len(expected),
        actual=len(actual),
        missing=tuple(sorted(expected.keys() - actual.keys())),
        extra=tuple(sorted(actual.keys() - expected.keys())),
        changed=tuple(sorted(k for k in expected.keys() & actual.keys() if expected[k] != actual[k])),
    )


def local_documents(root: Path) -> dict[str, str]:
    """knowledge/ 의 *.md → {버킷 키: MD5}. sync_knowledge_s3.sh 가 올리는 것과 같은 집합이다."""
    return {p.relative_to(root).as_posix(): hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*.md"))}


def s3_documents(objects: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """list-objects-v2 의 Contents → {키: ETag}. terraform 이 만든 접두사 자리표시(rules/aws/ 등)는 뺀다."""
    return {
        str(o["Key"]): str(o["ETag"]).strip('"')
        for o in objects
        if str(o["Key"]).endswith(".md")
    }


def expected_points(chunks: Sequence[Chunk], point_id: Callable[[str], str]) -> dict[str, str]:
    """색인해야 할 청크 → {point ID: 본문}. 본문까지 비교해야 섹션 내용만 고친 경우도 잡힌다."""
    return {point_id(c.chunk_id): c.text for c in chunks}
