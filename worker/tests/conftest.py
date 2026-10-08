from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.messages import build_review_requested
from review_ai.retrieval.file_retriever import FileRetriever
from review_common.repository import InMemoryReviewRepository
from review_worker.graph import Deps, build_graph
from review_worker.handler import ReviewHandler

SAMPLES = Path(__file__).resolve().parents[2] / "ai" / "samples"
MERGE_SHA = "f" * 40


def load_sample(name: str) -> dict[str, Any]:
    return yaml.safe_load((SAMPLES / name).read_text(encoding="utf-8"))


class FakeGitHub:
    def __init__(self) -> None:
        self.pulls: dict[str, list[dict[str, Any]]] = {}
        self.merged: list[tuple[str, int, str]] = []

    def open_pr(self, repository: str, head_sha: str, number: int = 7) -> None:
        self.pulls[head_sha] = [{"number": number, "state": "open", "head": {"sha": head_sha}}]

    async def pulls_for_commit(self, repository: str, sha: str) -> list[dict[str, Any]]:
        return self.pulls.get(sha, [])

    async def merge_pull(self, repository: str, number: int, *, head_sha: str) -> str:
        self.merged.append((repository, number, head_sha))
        return MERGE_SHA


class Harness:
    """API 가 하는 일(DB received 저장 + review.requested 발행)을 흉내 내고 워커 핸들러에 바로 넘긴다."""

    def __init__(self, llm: Any = None, **deps: Any) -> None:
        self.repo = InMemoryReviewRepository()
        self.github = FakeGitHub()
        llm = llm if llm is not None else ScriptedLLM(oracle_review)
        self.deps = Deps(repo=self.repo, github=self.github, llm=llm, retriever=FileRetriever(),
                         retry_backoff_seconds=0, **deps)
        self.graph = build_graph(self.deps, InMemorySaver())
        self.handler = ReviewHandler(self.repo, self.graph)

    async def request(self, spec: dict[str, Any], review_id: str = "rv_20261008_test") -> bytes:
        repository = spec["metadata"]["repository"]
        head_sha = "a" * 40
        spec_ref = {"repository": repository, "commit": head_sha, "path": "deploy.yaml"}
        msg = build_review_requested(spec, review_id=review_id, spec_ref=spec_ref, requested_by="tester",
                                     requested_at=datetime.now(UTC))
        await self.repo.insert_review(review_id=review_id, app=msg.app, target_env=msg.target_env,
                                      repo_id=msg.repo_id, spec_ref=spec_ref, pr_head_sha=head_sha,
                                      requested_by="tester")
        self.github.open_pr(repository, head_sha)
        raw = msg.model_dump_json().encode()
        await self.handler.handle("review.requested", raw)
        return raw

    async def resume(self, payload: dict[str, Any]) -> None:
        import json

        body = {"schema_version": "review.resumed/v1", "resumed_at": datetime.now(UTC).isoformat(), **payload}
        await self.handler.handle("review.resumed", json.dumps(body).encode())

    async def ci(self, review_id: str, conclusion: str = "success", head_sha: str = "a" * 40) -> None:
        await self.resume({"review_id": review_id, "kind": "ci_completed",
                           "ci": {"head_sha": head_sha, "conclusion": conclusion}})

    async def human(self, review_id: str, decision: str, edited_ops: list[dict[str, Any]] | None = None) -> None:
        await self.resume({"review_id": review_id, "kind": "human_decision",
                           "human_decision": {"decision": decision, "approver": "hyeyeon",
                                              "edited_ops": edited_ops or []}})

    def row(self, review_id: str = "rv_20261008_test") -> dict[str, Any]:
        return self.repo.reviews[review_id]


@pytest.fixture
def harness() -> Harness:
    return Harness()
