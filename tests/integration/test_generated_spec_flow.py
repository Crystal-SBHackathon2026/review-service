"""intake 가 만든 명세 → Review API → 워커 — 메모리 저장소로 API 와 워커를 같은 DB 처럼 잇는다 (Postgres·Kafka 불필요).

10/09 sample-app #11: deploy.yaml 을 지운 PR 에 intake 가 baseline 없이(레포 분석만으로) 명세를 만들었고,
그 명세가 network: {} 라 검토가 pass → 자동 병합 → gitops 의 ingress 가 지워져 ALB 가 삭제됐다.
baseline 없이 생성한 명세는 공개 범위·env·replicas 를 모르므로 pass 여도 사람 확인(GENERATED_SPEC_UNVERIFIED)으로 보낸다.

baseline 없는 생성 커밋은 intake 가 남기는 그대로(spec_intakes 행 + PR 브랜치 커밋) 넣어 둔다 — intake 가 어떤 명세를
커밋할지(#39 는 network 를 모르면 커밋하지 않는다)와 상관없이, 커밋된 뒤의 검토 층만 확인한다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.messages import ReviewRequested
from review_ai.retrieval.file_retriever import FileRetriever
from review_api.app import ApiDeps, create_app
from review_common.github import RefConflict, SpecNotFound
from review_common.repository import InMemoryReviewRepository
from review_worker.graph import Deps, build_graph
from review_worker.handler import ReviewHandler

SAMPLES = Path(__file__).resolve().parents[2] / "ai" / "samples"
REPO = "Crystal-SBHackathon2026/sample-app"
HEAD = "a" * 40
MERGE_SHA = "c0ffee1" + "0" * 33
SECRET = "flow-secret"
APP_TREE = {  # PR head — deploy.yaml 을 지운 앱 레포
    "Dockerfile": "FROM node:22-alpine\nEXPOSE 8080\nHEALTHCHECK CMD wget -qO- http://127.0.0.1:8080/healthz\n",
    "package.json": '{"dependencies": {"express": "^4.21.2"}}',
    ".github/workflows/ci.yml": "env:\n  IMAGE: ghcr.io/crystal-sbhackathon2026/sample-app\n"
                                "jobs:\n  b:\n    runs-on: ubuntu-latest\n",
    "src/server.js": "const port = Number(process.env.PORT || 8080);\n",
}
CI_DONE = [{"status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}]


class FakeGitHub:
    """앱 레포 하나 — 커밋별 파일, PR 브랜치(feature) 하나, check suite. API(intake)와 워커가 같이 쓴다."""

    def __init__(self) -> None:
        self.trees: dict[str, dict[str, str]] = {HEAD: dict(APP_TREE)}
        self.parents: dict[str, str] = {}
        self.branch = HEAD
        self.merged: list[str] = []

    # --- 읽기 (API specs·intake, 워커) ---
    async def get_file(self, repository: str, path: str, ref: str) -> str:
        try:
            return self.trees[ref][path]
        except KeyError:
            raise SpecNotFound(f"{repository}@{ref} 에 {path} 가 없다", 404) from None

    async def list_files(self, repository: str, ref: str) -> list[str]:
        return list(self.trees[ref])

    # --- 쓰기 ---
    async def prepare_files_commit(self, repository: str, *, parent: str, files: dict[str, str | None],
                                   message: str) -> str:
        sha = f"{len(self.parents) + 1:x}".rjust(40, "e")
        tree = {**self.trees[parent], **files}
        self.trees[sha] = {path: content for path, content in tree.items() if content is not None}
        self.parents[sha] = parent
        return sha

    async def prepare_file_commit(self, repository: str, *, parent: str, path: str, content: str,
                                  message: str) -> str:
        return await self.prepare_files_commit(repository, parent=parent, files={path: content}, message=message)

    async def update_branch(self, repository: str, branch: str, sha: str) -> None:
        if self.parents.get(sha) != self.branch:
            raise RefConflict("non-fast-forward", 422)
        self.branch = sha

    async def create_commit_status(self, repository: str, sha: str, **_: Any) -> None:
        pass

    # --- 워커 CI·병합 ---
    async def check_suites(self, repository: str, sha: str) -> list[dict[str, Any]]:
        return CI_DONE  # CI 는 이미 끝났다 — 검토가 통과하면 바로 병합으로 간다

    async def pulls_for_commit(self, repository: str, sha: str) -> list[dict[str, Any]]:
        if sha != self.branch:
            return []
        return [{"number": 11, "state": "open", "head": {"sha": sha, "ref": "feature", "repo": {"full_name": REPO}}}]

    async def merge_pull(self, repository: str, number: int, *, head_sha: str) -> str:
        self.merged.append(head_sha)
        return MERGE_SHA


class Publisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []

    async def send(self, topic: str, key: str, value: bytes) -> None:
        self.sent.append((topic, value))


class Flow:
    def __init__(self) -> None:
        self.repo = InMemoryReviewRepository()
        self.github = FakeGitHub()
        self.publisher = Publisher()
        self.client = TestClient(create_app(ApiDeps(
            repo=self.repo, specs=self.github, publisher=self.publisher, github_webhook_secret=SECRET,
            github=self.github, default_target="aws/ap-northeast-2", intake_repositories=frozenset({REPO}))))
        graph = build_graph(Deps(repo=self.repo, github=self.github, publisher=self.publisher,
                                 llm=ScriptedLLM(oracle_review), retriever=FileRetriever(), retry_backoff_seconds=0),
                            InMemorySaver())
        self.handler = ReviewHandler(self.repo, graph)

    def pr(self, action: str, sha: str) -> dict[str, Any]:
        event = {"action": action, "sender": {"login": "hyeyeon"}, "repository": {"full_name": REPO,
                                                                                    "default_branch": "main"},
                 "pull_request": {"number": 11, "base": {"ref": "main", "repo": {"full_name": REPO}},
                                  "head": {"sha": sha, "ref": "feature", "repo": {"full_name": REPO}}}}
        raw = json.dumps(event).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        resp = self.client.post("/webhooks/github", content=raw,
                                headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig})
        assert resp.status_code == 202, resp.text
        return resp.json()

    async def deliver(self) -> list[ReviewRequested]:
        """API·워커가 발행한 review.requested 를 워커에 넣는다. 넣은 메시지들."""
        delivered = []
        while self.publisher.sent:
            pending, self.publisher.sent = self.publisher.sent, []
            for topic, value in pending:
                if topic == "review.requested":
                    delivered.append(ReviewRequested.model_validate_json(value))
                await self.handler.handle(topic, value)
        return delivered

    async def human(self, review_id: str, decision: str = "approved") -> None:
        body = {"schema_version": "review.resumed/v1", "resumed_at": datetime.now(UTC).isoformat(),
                "review_id": review_id, "kind": "human_decision",
                "human_decision": {"decision": decision, "approver": "hyeyeon", "edited_ops": [],
                                   "use_recommendations": False}}
        await self.handler.handle("review.resumed", json.dumps(body).encode())

    async def generated_review(self) -> dict[str, Any]:
        """deploy.yaml 없는 PR → intake 생성 커밋 → synchronize 웹훅 → 워커 검토. 그 검토 행.

        baseline 이 있으면 실제 intake 가 만들고, 없으면 레포 분석으로 만든 명세(network: {})를 intake 처럼 커밋해 둔다."""
        if await self.repo.latest_baseline_for_repository(REPO) is not None:
            assert "kind" in self.pr("opened", HEAD)  # TestClient 가 BackgroundTasks(process_intake)까지 돌린다
        else:
            await self._commit_like_intake_without_baseline()
        [intake] = self.repo.intakes.values()
        assert intake["status"] == "generated", intake
        body = self.pr("synchronize", intake["result_commit_sha"])
        assert body["from_intake"] == intake["intake_id"]
        await self.deliver()
        return self.repo.reviews[body["review_id"]]

    async def _commit_like_intake_without_baseline(self) -> None:
        spec = yaml.safe_load((SAMPLES / "01-pass-sample-app-aws.yaml").read_text(encoding="utf-8"))
        spec["network"] = {}  # 장애 때의 생성 명세 — 공개 범위를 몰라 내부 전용이 됐다
        await self.repo.insert_intake(intake_id="in_gen", repository=REPO, head_repository=REPO, pr_number=11,
                                      head_sha=HEAD, head_ref="feature", path="deploy.yaml", kind="missing",
                                      errors=[], requested_by="hyeyeon")
        sha = await self.github.prepare_file_commit(REPO, parent=HEAD, path="deploy.yaml",
                                                    content=yaml.safe_dump(spec), message="chore: 생성")
        await self.repo.link_intake("in_gen", result_commit_sha=sha, baseline_used=False)
        await self.github.update_branch(REPO, "feature", sha)
        await self.repo.finish_intake("in_gen", status="generated", reason="GENERATED", message="m",
                                      result_commit_sha=sha)


async def _approve_baseline(flow: Flow) -> None:
    spec = yaml.safe_load((SAMPLES / "01-pass-sample-app-aws.yaml").read_text(encoding="utf-8"))
    await flow.repo.upsert_baseline(app="sample-app", target_env="aws", spec=spec, merge_sha="9" * 40,
                                    spec_ref={"repository": REPO, "commit": "9" * 40, "path": "deploy.yaml"},
                                    observed_at=datetime.now(UTC))


async def test_spec_generated_without_baseline_waits_for_human_even_when_it_passes() -> None:
    """재현: 고치기 전에는 이 검토가 pass → 병합(merging → commit_overlay)까지 갔다."""
    flow = Flow()
    row = await flow.generated_review()

    assert flow.github.merged == []  # 사람 확인 전에는 병합하지 않는다
    assert (row["status"], row["verdict"]) == ("needs_human", "needs_human")
    assert row["reasons"] == ["GENERATED_SPEC_UNVERIFIED"]
    detail = flow.client.get(f"/reviews/{row['review_id']}").json()
    assert "baseline 없이 생성된 명세" in detail["reason_messages"]["GENERATED_SPEC_UNVERIFIED"]

    await flow.human(row["review_id"])  # 사람이 공개 범위·env·replicas 를 확인하고 승인하면 병합된다
    assert flow.github.merged == [row["pr_head_sha"]]
    assert flow.repo.reviews[row["review_id"]]["status"] == "blocked"  # commit_overlay 기본값(stub)


async def test_spec_generated_from_baseline_is_merged_as_before() -> None:
    flow = Flow()
    await _approve_baseline(flow)
    row = await flow.generated_review()

    assert (row["status"], row["verdict"], row["reasons"]) == ("blocked", "pass", [])
    assert flow.github.merged == [row["pr_head_sha"]]


async def test_later_commit_on_the_generated_pr_still_waits_for_human() -> None:
    """생성 커밋 위에 앱 코드만 바꾼 커밋이 와도 명세는 여전히 baseline 없이 만든 것이다."""
    flow = Flow()
    first = await flow.generated_review()
    sha = await flow.github.prepare_file_commit(REPO, parent=first["pr_head_sha"], path="src/server.js",
                                                content="// changed\n", message="m")
    await flow.github.update_branch(REPO, "feature", sha)

    body = flow.pr("synchronize", sha)
    await flow.deliver()

    row = flow.repo.reviews[body["review_id"]]
    assert (row["status"], row["reasons"]) == ("needs_human", ["GENERATED_SPEC_UNVERIFIED"])
    assert flow.repo.reviews[first["review_id"]]["status"] == "superseded"
    assert flow.github.merged == []
