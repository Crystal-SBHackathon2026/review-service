from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.messages import build_review_requested
from review_ai.retrieval.file_retriever import FileRetriever
from review_common.github import GitHubError
from review_common.repository import InMemoryReviewRepository
from review_worker.graph import Deps, build_graph
from review_worker.handler import ReviewHandler

SAMPLES = Path(__file__).resolve().parents[2] / "ai" / "samples"
HEAD = "a" * 40
MERGE_SHA = "f" * 40
FIX_SHA = "b" * 40  # AI 수정 커밋 SHA


def load_sample(name: str) -> dict[str, Any]:
    return yaml.safe_load((SAMPLES / name).read_text(encoding="utf-8"))


def suite(status: str = "completed", conclusion: str | None = "success", slug: str = "github-actions") -> dict:
    return {"status": status, "conclusion": conclusion, "app": {"slug": slug}}


class FakeGitHub:
    """앱 레포 하나를 흉내 낸다 — 커밋별 deploy.yaml, PR, check suite."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}  # commit → deploy.yaml
        self.pulls: dict[str, list[dict[str, Any]]] = {}
        self.suites: dict[str, list[dict[str, Any]]] = {}
        self.suite_error: GitHubError | None = None
        self.branch_error: GitHubError | None = None
        self.merged: list[tuple[str, int, str]] = []
        self.commits: list[dict[str, Any]] = []  # 브랜치에 실제로 올라간 커밋
        self.prepared: dict[str, dict[str, Any]] = {}  # 만들었지만 브랜치를 아직 안 옮긴 커밋

    def open_pr(self, repository: str, head_sha: str, number: int = 7, *, fork: bool = False) -> None:
        head_repo = "someone/fork" if fork else repository
        self.pulls[head_sha] = [{"number": number, "state": "open",
                                 "head": {"sha": head_sha, "ref": "feature", "repo": {"full_name": head_repo}}}]

    async def get_file(self, repository: str, path: str, ref: str) -> str:
        return self.files[ref]

    async def prepare_file_commit(self, repository: str, *, parent: str, path: str, content: str,
                                  message: str) -> str:
        self.prepared[FIX_SHA] = {"parent": parent, "path": path, "content": content, "message": message}
        return FIX_SHA

    async def update_branch(self, repository: str, branch: str, sha: str) -> None:
        if self.branch_error:
            raise self.branch_error
        commit = self.prepared.pop(sha)
        self.commits.append({"branch": branch, **commit})
        self.files[sha] = commit["content"]
        for pull in self.pulls.pop(commit["parent"], []):  # 브랜치 head 가 새 커밋으로 움직인다
            pull["head"]["sha"] = sha
            self.pulls[sha] = [pull]

    async def check_suites(self, repository: str, sha: str) -> list[dict[str, Any]]:
        if self.suite_error:
            raise self.suite_error
        return self.suites.get(sha, [])

    async def pulls_for_commit(self, repository: str, sha: str) -> list[dict[str, Any]]:
        return self.pulls.get(sha, [])

    async def merge_pull(self, repository: str, number: int, *, head_sha: str) -> str:
        self.merged.append((repository, number, head_sha))
        return MERGE_SHA


class FakePublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bytes]] = []

    async def send(self, topic: str, key: str, value: bytes) -> None:
        self.sent.append((topic, key, value))


class Harness:
    """API 가 하는 일(DB received 저장 + review.requested 발행)을 흉내 내고 워커 핸들러에 바로 넘긴다."""

    def __init__(self, llm: Any = None, **deps: Any) -> None:
        self.repo = InMemoryReviewRepository()
        self.github = FakeGitHub()
        self.publisher = FakePublisher()
        llm = llm if llm is not None else ScriptedLLM(oracle_review)
        self.deps = Deps(repo=self.repo, github=self.github, publisher=self.publisher, llm=llm,
                         retriever=FileRetriever(), retry_backoff_seconds=0, **deps)
        self.graph = build_graph(self.deps, InMemorySaver())
        self.handler = ReviewHandler(self.repo, self.graph)

    async def request(self, spec: dict[str, Any], review_id: str = "rv_20261008_test") -> bytes:
        repository = spec["metadata"]["repository"]
        spec_ref = {"repository": repository, "commit": HEAD, "path": "deploy.yaml"}
        msg = build_review_requested(spec, review_id=review_id, spec_ref=spec_ref, requested_by="tester",
                                     requested_at=datetime.now(UTC))
        await self.repo.insert_review(review_id=review_id, app=msg.app, target_env=msg.target_env,
                                      repo_id=msg.repo_id, spec_ref=spec_ref, pr_head_sha=HEAD,
                                      requested_by="tester")
        self.github.files.setdefault(HEAD, yaml.safe_dump(spec, sort_keys=False))
        if HEAD not in self.github.pulls:
            self.github.open_pr(repository, HEAD)
        raw = msg.model_dump_json().encode()
        await self.handler.handle("review.requested", raw)
        return raw

    async def deliver_published(self) -> None:
        """워커가 발행한 review.requested(AI 수정 커밋 재검토)를 워커에 다시 넣는다."""
        pending, self.publisher.sent = self.publisher.sent, []
        for topic, _, value in pending:
            await self.handler.handle(topic, value)

    async def resume(self, payload: dict[str, Any]) -> None:
        body = {"schema_version": "review.resumed/v1", "resumed_at": datetime.now(UTC).isoformat(), **payload}
        await self.handler.handle("review.resumed", json.dumps(body).encode())

    async def ci(self, review_id: str, conclusion: str = "success", head_sha: str = HEAD) -> None:
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
