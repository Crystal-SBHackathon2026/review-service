from __future__ import annotations

from review_ai.retrieval.evaluation import (
    RetrievalCase,
    load_retrieval_cases,
    run_retrieval_case,
    summarize_retrieval,
)
from review_ai.retrieval.knowledge import load_chunks
from review_ai.state import Doc


class FixedRanker:
    """질문과 무관하게 정해 둔 순위를 돌려준다 — 지표 계산만 본다."""

    def __init__(self, ranked: list[tuple[str, float]]) -> None:
        self._ranked = ranked

    async def rank(self, query: str, target_env: str, *, limit: int) -> list[Doc]:
        return [
            Doc(chunk_id=f"{uri}#{i}", rule_id=None, doc_type="incident", provider="any", score=score,  # type: ignore[typeddict-item]
                match="semantic", source_uri=uri, text="")  # type: ignore[typeddict-item]
            for i, (uri, score) in enumerate(self._ranked[:limit])
        ]


def case(expect: tuple[str, ...], *, rule: str | None = None, kind: str = "symptom") -> RetrievalCase:
    return RetrievalCase("c", kind, "질문", "aws", expect, rule)


async def test_hit_rank_and_served_with_threshold() -> None:
    ranker = FixedRanker([("a.md", 0.9), ("b.md", 0.45), ("c.md", 0.4)])
    hit = await run_retrieval_case(case(("b.md",)), ranker, min_score=0.5)
    assert (hit.rank, hit.hit(1), hit.hit(3), hit.best_score) == (2, False, True, 0.45)
    assert hit.served == ("a.md",) and not hit.ok  # 상위 3개엔 있지만 임계값에 걸려 프롬프트엔 못 들어간다
    summary = summarize_retrieval([hit])
    assert summary["hit@3"] == 1.0 and summary["served@3"] == 0.0 and summary["dropped_by_threshold"] == ["c"]


async def test_negative_case_fails_only_when_something_passes_threshold() -> None:
    quiet = await run_retrieval_case(case(()), FixedRanker([("a.md", 0.4)]), min_score=0.5)
    noisy = await run_retrieval_case(case(()), FixedRanker([("a.md", 0.6)]), min_score=0.5)
    assert quiet.ok and not noisy.ok
    assert summarize_retrieval([quiet, noisy])["negative_false_hits"] == 1


async def test_miss_covered_by_exact_rule_is_reported_separately() -> None:
    ranker = FixedRanker([("x.md", 0.9)])
    expect = ("guides/kafka-secret-masking.md",)  # related_rules: [SEC-001]
    covered = await run_retrieval_case(case(expect, rule="SEC-001", kind="finding"), ranker, min_score=0.5,
                                       chunks=load_chunks())
    uncovered = await run_retrieval_case(case(expect, rule="DB-003", kind="finding"), ranker, min_score=0.5,
                                         chunks=load_chunks())
    assert covered.covered_by_rule and not uncovered.covered_by_rule
    summary = summarize_retrieval([covered])
    assert summary["misses"] == [] and summary["misses_covered_by_rule"] == ["c"]


def test_retrieval_cases_point_to_existing_documents() -> None:
    uris = {c.source_uri for c in load_chunks()}
    semantic = {c.source_uri for c in load_chunks() if c.doc_type in ("incident", "guide")}
    cases = load_retrieval_cases()
    assert len({c.case_id for c in cases}) == len(cases)
    for c in cases:
        assert set(c.expect) <= semantic <= uris, c.case_id  # 의미 검색 대상(사례·가이드)만 정답이 될 수 있다
        assert c.env in ("aws", "gcp", "local")
    assert {c.kind for c in cases} == {"symptom", "finding", "negative"}
    assert {c.source_uri for c in load_chunks() if c.doc_type == "incident"} <= {u for c in cases for u in c.expect}
