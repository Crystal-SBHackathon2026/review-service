from __future__ import annotations

from collections import Counter
from typing import get_args

import pytest
import yaml
from qdrant_client import AsyncQdrantClient

from review_ai.catalog import load_rules
from review_ai.overlay.warnings import BLOCKING, WarningCode, warning_doc_uri
from review_ai.retrieval import make_retrieve_evidence
from review_ai.retrieval.embedding import HashEmbedder
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.retrieval.knowledge import FRONT_MATTER, KNOWLEDGE_DIR, load_chunks, parse_document
from review_ai.retrieval.qdrant_retriever import (
    QdrantRetriever,
    index_chunks,
    indexed_points,
    point_id,
    prune_points,
)
from review_ai.retrieval.sync_check import compare, expected_points, local_documents, s3_documents
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import run_static_check
from review_ai.static_check.rules import CHECKS
from tests.conftest import load_sample_dict


def findings_for(name: str):
    return run_static_check(DeploySpec.model_validate(load_sample_dict(name)))


def test_every_p0_and_implemented_rule_has_a_document() -> None:
    p0 = {r.id for r in load_rules().values() if r.priority == "P0"}
    documented = {rid for c in load_chunks() if c.doc_type == "rule" for rid in c.rule_ids}
    assert p0 <= documented
    assert set(CHECKS) <= documented  # 구현한 규칙에 근거 문서가 없으면 judge 인용이 깨진다


def test_chunk_counts_per_env_meet_target() -> None:
    chunks = load_chunks()
    per_env = Counter()
    for env in ("aws", "gcp", "local"):
        per_env[env] = sum(1 for c in chunks if c.provider in ("any", env))
    assert all(30 <= n for n in per_env.values()), per_env


def test_all_related_rules_exist() -> None:
    rules = load_rules()
    for chunk in load_chunks():
        assert set(chunk.rule_ids) <= set(rules), chunk.chunk_id


def test_parse_document_requires_front_matter() -> None:
    with pytest.raises(ValueError):
        parse_document("## 제목만\n본문", "x.md")


async def test_file_retriever_returns_exact_docs_for_env() -> None:
    docs = await FileRetriever().search(findings_for("05-fix-engine-unsupported-local.yaml"), "local")
    uris = {d["source_uri"] for d in docs}
    assert "rules/any/DB-002.md" in uris and "rules/local/DB-002.md" in uris
    assert "rules/aws/DB-002.md" not in uris
    assert all(d["match"] == "exact_rule" and d["score"] == 1.0 and d["rule_id"] == "DB-002" for d in docs)


async def test_file_retriever_includes_related_incidents() -> None:
    docs = await FileRetriever().search(findings_for("10-human-mixed-aws.yaml"), "aws")
    assert any(d["doc_type"] == "incident" and d["rule_id"] == "RUN-001" for d in docs)
    assert len({d["chunk_id"] for d in docs}) == len(docs)


async def test_node_skips_search_without_findings() -> None:
    node = make_retrieve_evidence(FileRetriever())
    assert await node({"findings": [], "target_env": "aws"}) == {"retrieved_docs": []}


@pytest.fixture
async def qdrant() -> QdrantRetriever:
    client = AsyncQdrantClient(location=":memory:")
    embedder = HashEmbedder()
    await index_chunks(client, embedder, load_chunks())
    return QdrantRetriever(client, embedder, min_score=0.0)  # 해시 임베딩은 점수가 낮다


async def test_qdrant_exact_matches_file_retriever(qdrant: QdrantRetriever) -> None:
    findings = findings_for("05-fix-engine-unsupported-local.yaml")
    exact = {d["chunk_id"] for d in await qdrant.search(findings, "local") if d["match"] == "exact_rule"}
    expected = {d["chunk_id"] for d in await FileRetriever().search(findings, "local")}
    assert exact == expected


async def test_qdrant_semantic_returns_only_incidents_and_guides(qdrant: QdrantRetriever) -> None:
    docs = await qdrant.search(findings_for("07-fix-public-bucket.yaml"), "aws")
    semantic = [d for d in docs if d["match"] == "semantic"]
    assert semantic and all(d["doc_type"] in ("incident", "guide") and d["rule_id"] is None for d in semantic)
    assert all(0.0 <= d["score"] <= 1.0 for d in semantic)


async def test_reindex_is_idempotent(qdrant: QdrantRetriever) -> None:
    client = qdrant._client
    before = (await client.count("review-knowledge")).count
    await index_chunks(client, HashEmbedder(), load_chunks())
    assert (await client.count("review-knowledge")).count == before


async def test_qdrant_drops_weak_semantic_hits() -> None:
    client = AsyncQdrantClient(location=":memory:")
    await index_chunks(client, HashEmbedder(), load_chunks())
    strict = QdrantRetriever(client, HashEmbedder(), min_score=0.99)
    docs = await strict.search(findings_for("07-fix-public-bucket.yaml"), "aws")
    assert docs and all(d["match"] == "exact_rule" for d in docs)


def test_every_render_warning_code_has_a_document() -> None:
    by_code = {c.warning_code: c for c in load_chunks() if c.doc_type == "warning"}
    assert set(by_code) == set(get_args(WarningCode))
    for code, chunk in by_code.items():
        assert chunk.source_uri == warning_doc_uri(code)
        assert chunk.rule_ids == ()  # 규칙 정확 매칭에 섞여 judge 프롬프트로 들어가지 않게


def test_warning_document_blocking_matches_renderer() -> None:
    for path in sorted((KNOWLEDGE_DIR / "warnings").glob("*.md")):
        meta = yaml.safe_load(FRONT_MATTER.match(path.read_text(encoding="utf-8")).group(1))
        assert meta["blocking"] is BLOCKING[meta["warning_code"]], path.name  # 문서와 렌더러가 따로 놀지 않게


async def test_retrievers_never_return_warning_documents(qdrant: QdrantRetriever) -> None:
    findings = findings_for("10-human-mixed-aws.yaml") + findings_for("02-pass-local-sqlite.yaml")
    for docs in (await FileRetriever().search(findings, "aws"), await qdrant.search(findings, "aws")):
        assert docs and all(d["doc_type"] != "warning" for d in docs)


async def test_prune_removes_points_for_deleted_sections(qdrant: QdrantRetriever) -> None:
    client = qdrant._client
    chunks = load_chunks()
    kept = chunks[:-2]
    stale = await prune_points(client, {point_id(c.chunk_id) for c in kept})
    assert stale == sorted(point_id(c.chunk_id) for c in chunks[-2:])
    assert set(await indexed_points(client)) == {point_id(c.chunk_id) for c in kept}
    assert await prune_points(client, {point_id(c.chunk_id) for c in kept}) == []


async def test_indexed_points_match_local_chunks(qdrant: QdrantRetriever) -> None:
    diff = compare("qdrant", expected_points(load_chunks(), point_id), await indexed_points(qdrant._client))
    assert diff.ok and diff.expected == diff.actual == len(load_chunks())


async def test_indexed_points_is_empty_without_collection() -> None:
    assert await indexed_points(AsyncQdrantClient(location=":memory:")) == {}


async def test_rank_returns_scored_semantic_chunks_without_threshold(qdrant: QdrantRetriever) -> None:
    docs = await qdrant.rank("SQLite 파일이 재시작하면 사라진다", "local", limit=5)
    assert len(docs) == 5 and all(d["doc_type"] in ("incident", "guide") for d in docs)
    assert [d["score"] for d in docs] == sorted((d["score"] for d in docs), reverse=True)


def test_sync_compare_reports_missing_extra_and_changed() -> None:
    diff = compare("s3", {"a.md": "1", "b.md": "2", "c.md": "3"}, {"a.md": "1", "b.md": "X", "d.md": "4"})
    assert (diff.ok, diff.missing, diff.extra, diff.changed) == (False, ("c.md",), ("d.md",), ("b.md",))
    assert diff.lines()[0].startswith("DIFF s3: 기준 3 · 대상 3")
    assert compare("s3", {"a.md": "1"}, {"a.md": "1"}).ok


def test_local_documents_hash_matches_s3_etag_format(tmp_path) -> None:
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "X.md").write_bytes(b"hello")
    (tmp_path / "notes.txt").write_text("md 가 아니면 올리지 않는다")
    assert local_documents(tmp_path) == {"rules/X.md": "5d41402abc4b2a76b9719d911017c592"}
    objects = [{"Key": "rules/", "ETag": '"d41d8cd98f00b204e9800998ecf8427e"'},
               {"Key": "rules/X.md", "ETag": '"5d41402abc4b2a76b9719d911017c592"'}]
    assert compare("s3", local_documents(tmp_path), s3_documents(objects)).ok
