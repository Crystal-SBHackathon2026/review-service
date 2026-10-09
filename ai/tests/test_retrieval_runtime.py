from __future__ import annotations

import pytest
from qdrant_client import AsyncQdrantClient

from review_ai.retrieval import embedding
from review_ai.retrieval.embedding import HashEmbedder
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.retrieval.indexer import index, parse_args, run, wait_ready
from review_ai.retrieval.knowledge import load_chunks
from review_ai.retrieval.qdrant_retriever import QdrantRetriever, indexed_points
from review_ai.retrieval.runtime import OptionalRetriever, make_retriever, qdrant_from_url
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import run_static_check
from tests.conftest import load_sample_dict


def findings(sample: str) -> list:
    return run_static_check(DeploySpec.model_validate(load_sample_dict(sample)))


class Broken:
    async def search(self, findings, target_env):
        raise ConnectionError("qdrant down")


async def test_without_qdrant_is_the_file_retriever() -> None:
    found = findings("03-fix-sqlite-replicas-gcp.yaml")
    assert await make_retriever().search(found, "gcp") == await FileRetriever().search(found, "gcp")


async def test_qdrant_failure_keeps_rule_documents() -> None:
    found = findings("03-fix-sqlite-replicas-gcp.yaml")
    docs = await make_retriever(qdrant=Broken()).search(found, "gcp")
    assert docs == await FileRetriever().search(found, "gcp")


async def test_optional_retriever_returns_empty_on_error() -> None:
    assert await OptionalRetriever(Broken(), "Qdrant").search(findings("03-fix-sqlite-replicas-gcp.yaml"), "gcp") == []


async def test_empty_qdrant_after_restart_keeps_rule_documents() -> None:
    """emptyDir Qdrant 가 다시 떠서 컬렉션이 아직 없을 때 — 파일 검색 결과 그대로."""
    found = findings("03-fix-sqlite-replicas-gcp.yaml")
    qdrant = QdrantRetriever(AsyncQdrantClient(location=":memory:"), HashEmbedder(), min_score=0.0)
    assert await make_retriever(qdrant=qdrant).search(found, "gcp") == await FileRetriever().search(found, "gcp")


async def test_qdrant_adds_semantic_docs_after_rule_documents() -> None:
    found = findings("03-fix-sqlite-replicas-gcp.yaml")
    client = AsyncQdrantClient(location=":memory:")
    embedder = HashEmbedder()
    await index(client, embedder, load_chunks())
    docs = await make_retriever(qdrant=QdrantRetriever(client, embedder, min_score=0.0)).search(found, "gcp")
    exact = await FileRetriever().search(found, "gcp")
    assert docs[:len(exact)] == exact  # 규칙 문서가 먼저, 순서도 그대로
    extra = docs[len(exact):]
    assert extra and {d["match"] for d in extra} == {"semantic"}
    assert len({d["chunk_id"] for d in docs}) == len(docs)


async def test_extra_retrievers_come_last() -> None:
    class Cases:
        async def search(self, findings, target_env):
            return [{"chunk_id": "case:x", "rule_id": "DB-001", "doc_type": "case", "provider": "gcp", "score": 1.0,
                     "match": "exact_rule", "source_uri": "review://x", "text": "t"}]

    docs = await make_retriever(Cases()).search(findings("03-fix-sqlite-replicas-gcp.yaml"), "gcp")
    assert docs[-1]["chunk_id"] == "case:x"


def test_qdrant_from_url_without_url_is_none() -> None:
    assert qdrant_from_url(None) is None
    assert qdrant_from_url("") is None


def test_qdrant_from_url_survives_model_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("model download blocked")

    monkeypatch.setattr(embedding, "FastEmbedder", boom)
    assert qdrant_from_url("http://qdrant:6333") is None


def test_qdrant_from_url_builds_retriever(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedding, "FastEmbedder", HashEmbedder)
    assert isinstance(qdrant_from_url("http://qdrant:6333"), QdrantRetriever)


async def test_index_is_idempotent_and_prunes() -> None:
    client = AsyncQdrantClient(location=":memory:")
    chunks = load_chunks()
    first = await index(client, HashEmbedder(), chunks)
    assert f"{len(chunks)}개 청크 색인 · 남은 청크 0개 삭제" in first
    second = await index(client, HashEmbedder(), chunks[:-1])
    assert "남은 청크 1개 삭제" in second
    assert len(await indexed_points(client)) == len(chunks) - 1


async def test_index_without_prune_keeps_points() -> None:
    client = AsyncQdrantClient(location=":memory:")
    chunks = load_chunks()
    await index(client, HashEmbedder(), chunks)
    await index(client, HashEmbedder(), chunks[:-1], prune=False)
    assert len(await indexed_points(client)) == len(chunks)


async def test_wait_ready_returns_when_qdrant_answers() -> None:
    await wait_ready(AsyncQdrantClient(location=":memory:"), 0)


async def test_wait_ready_retries_then_gives_up() -> None:
    calls = []

    class Down:
        async def get_collections(self):
            calls.append(1)
            raise ConnectionError("refused")

    with pytest.raises(ConnectionError):
        await wait_ready(Down(), 0.05, interval=0.01)
    assert len(calls) >= 2


def test_parse_args_defaults_to_packaged_knowledge() -> None:
    args = parse_args(["--qdrant-url", "http://qdrant:6333", "--wait", "120"])
    assert args.knowledge_dir.is_dir() and args.wait == 120 and not args.no_prune


def test_parse_args_rejects_missing_dir(tmp_path) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--qdrant-url", "http://q", "--knowledge-dir", str(tmp_path / "nope")])


async def test_run_rejects_empty_knowledge_dir(tmp_path) -> None:
    with pytest.raises(SystemExit, match="색인할 문서가 없다"):
        await run(["--qdrant-url", "http://q", "--knowledge-dir", str(tmp_path)])
