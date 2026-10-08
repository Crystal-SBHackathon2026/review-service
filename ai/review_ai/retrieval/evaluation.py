"""검색 평가 — eval/retrieval_cases.yaml 의 "질문 → 나와야 할 문서" 짝으로 의미 검색을 잰다.

워커는 finding 하나에 의미 검색 상위 SEMANTIC_LIMIT(3)개 청크를 받고, 그중 임계값(SEMANTIC_MIN_SCORE) 미만은 버린다.
그래서 두 가지를 따로 본다.
  hit@k        임계값 없이 상위 k 청크 안에 정답 문서가 있는가 — 임베딩·문서 자체의 품질
  served@3     임계값까지 적용한 뒤 실제로 프롬프트에 들어갔을 상위 3개 안에 있는가 — 워커가 받는 결과
관련 문서가 없는 질문(expect: [])은 임계값을 넘는 결과가 하나라도 나오면 오탐이다.
finding 질문은 의미 검색이 놓쳐도 정답 문서가 ruleId 정확 매칭으로 이미 프롬프트에 들어갈 수 있다 — misses_covered_by_rule 로 따로 센다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from review_ai.retrieval.knowledge import Chunk
from review_ai.state import Doc

AI_ROOT = Path(__file__).resolve().parent.parent.parent
RETRIEVAL_CASES = AI_ROOT / "eval" / "retrieval_cases.yaml"
RANK_DEPTH = 10  # MRR·순위를 볼 깊이. 워커가 받는 건 3개뿐이다


class Ranker(Protocol):
    async def rank(self, query: str, target_env: str, *, limit: int) -> list[Doc]: ...


@dataclass(frozen=True)
class RetrievalCase:
    case_id: str
    kind: str  # symptom · finding · negative
    query: str
    env: str
    expect: tuple[str, ...]
    rule: str | None = None  # finding 질문의 ruleId


@dataclass(frozen=True)
class RetrievalResult:
    case_id: str
    kind: str
    expect: tuple[str, ...]
    rank: int | None  # 정답 문서의 첫 순위(1부터, RANK_DEPTH 안). 없으면 None
    best_score: float | None  # 정답 문서 청크의 최고 점수
    top: tuple[tuple[str, float], ...]  # 상위 3개 (문서, 점수)
    served: tuple[str, ...]  # 임계값 적용 후 상위 3개 문서
    covered_by_rule: bool = False  # 정답 문서가 ruleId 정확 매칭(related_rules)으로 이미 들어간다

    def hit(self, k: int) -> bool:
        return self.rank is not None and self.rank <= k

    @property
    def served_hit(self) -> bool:
        return any(uri in self.expect for uri in self.served)

    @property
    def ok(self) -> bool:
        return not self.served if not self.expect else self.served_hit


def load_retrieval_cases(path: Path = RETRIEVAL_CASES) -> list[RetrievalCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]
    return [RetrievalCase(c["id"], c["kind"], c["query"], c["env"], tuple(c["expect"]), c.get("rule")) for c in raw]


def _covered_by_rule(case: RetrievalCase, chunks: Sequence[Chunk]) -> bool:
    if case.rule is None or not case.expect:
        return False
    exact = {c.source_uri for c in chunks if case.rule in c.rule_ids and c.provider in ("any", case.env)}
    return any(uri in exact for uri in case.expect)


async def run_retrieval_case(case: RetrievalCase, ranker: Ranker, *, min_score: float, chunks: Sequence[Chunk] = (),
                             served_limit: int = 3) -> RetrievalResult:
    docs = await ranker.rank(case.query, case.env, limit=RANK_DEPTH)
    uris = [d["source_uri"] for d in docs]
    rank = next((i for i, uri in enumerate(uris, 1) if uri in case.expect), None)
    scores = [d["score"] for d in docs if d["source_uri"] in case.expect]
    return RetrievalResult(
        case_id=case.case_id,
        kind=case.kind,
        expect=case.expect,
        rank=rank,
        best_score=max(scores) if scores else None,
        top=tuple((d["source_uri"], round(d["score"], 3)) for d in docs[:served_limit]),
        served=tuple(d["source_uri"] for d in docs[:served_limit] if d["score"] >= min_score),
        covered_by_rule=_covered_by_rule(case, chunks),
    )


def summarize_retrieval(results: Sequence[RetrievalResult]) -> dict[str, object]:
    positive = [r for r in results if r.expect]
    negative = [r for r in results if not r.expect]

    def rate(items: Sequence[RetrievalResult], pred) -> float | None:
        return round(sum(1 for r in items if pred(r)) / len(items), 3) if items else None

    by_kind = {
        kind: {"cases": len(items), "hit@3": rate(items, lambda r: r.hit(3)), "served@3": rate(items, lambda r: r.served_hit)}
        for kind in sorted({r.kind for r in positive})
        if (items := [r for r in positive if r.kind == kind])
    }
    return {
        "cases": len(results),
        "positive": len(positive),
        "hit@1": rate(positive, lambda r: r.hit(1)),
        "hit@3": rate(positive, lambda r: r.hit(3)),
        "served@3": rate(positive, lambda r: r.served_hit),
        "mrr@10": round(sum(1 / r.rank for r in positive if r.rank) / len(positive), 3) if positive else None,
        "by_kind": by_kind,
        "negative": len(negative),
        "negative_false_hits": sum(1 for r in negative if r.served),
        "misses": [r.case_id for r in positive if not r.hit(3) and not r.covered_by_rule],
        "misses_covered_by_rule": [r.case_id for r in positive if not r.hit(3) and r.covered_by_rule],
        "dropped_by_threshold": [r.case_id for r in positive if r.hit(3) and not r.served_hit],
    }
