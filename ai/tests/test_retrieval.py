from __future__ import annotations

from collections import Counter

import pytest
from qdrant_client import AsyncQdrantClient

from review_ai.catalog import load_rules
from review_ai.retrieval import make_retrieve_evidence
from review_ai.retrieval.embedding import HashEmbedder
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.retrieval.knowledge import load_chunks, parse_document
from review_ai.retrieval.qdrant_retriever import QdrantRetriever, index_chunks
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
