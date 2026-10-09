"""review sweep — 멈춘 검토(received·reviewing·merging)를 회수한다 (P1-4). 워커까지 잇는 재현은 tests/integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from review_ai.messages import ReviewRequested
from review_api.app import ApiDeps
from review_api.recovery import MAX_RECOVERIES, recover_stale_reviews, stale_after
from review_common.repository import InMemoryReviewRepository
from review_common.resumed import CiCompletedResumed, RetryOverlayResumed, parse_review_resumed
from tests.test_api import HEAD, REPO, FakePublisher, FakeSpecs, sample_text

STALE = timedelta(minutes=10)
MERGE_SHA = "f" * 40


class FakeGitHub:
    def __init__(self) -> None:
        self.pulls: list[dict[str, Any]] = []
        self.statuses: list[tuple[str, str, str]] = []

    async def pulls_for_commit(self, repository: str, sha: str) -> list[dict[str, Any]]:
        return self.pulls

    async def create_commit_status(self, repository: str, sha: str, *, state: str, context: str, description: str,
                                   target_url: str | None = None) -> None:
        self.statuses.append((sha, context, state))


class Env:
    def __init__(self) -> None:
        self.repo = InMemoryReviewRepository()
        self.specs = FakeSpecs()
        self.publisher = FakePublisher()
        self.github = FakeGitHub()
        self.deps = ApiDeps(repo=self.repo, specs=self.specs, publisher=self.publisher,
                            github=self.github)  # type: ignore[arg-type]
        self.specs.files[(REPO, "deploy.yaml", HEAD)] = sample_text()

    async def review(self, rid: str, status: str, *, age: timedelta = STALE + timedelta(minutes=1),
                     requested_by: str = "octo-dev", pr_number: int | None = 5, **fields: Any) -> None:
        await self.repo.insert_review(review_id=rid, app="sample-app", target_env="aws", repo_id=REPO,
                                      spec_ref={"repository": REPO, "commit": HEAD, "path": "deploy.yaml"},
                                      pr_head_sha=HEAD, requested_by=requested_by, pr_number=pr_number)
        await self.repo.update_review(rid, status=status, **fields)
        self.repo.reviews[rid]["updated_at"] -= age

    async def sweep(self) -> list[dict[str, str]]:
        return await recover_stale_reviews(self.deps, STALE)

    def sent(self) -> list[tuple[str, Any]]:
        out = []
        for topic, _, value in self.publisher.sent:
            msg = ReviewRequested.model_validate_json(value) if topic == "review.requested" \
                else parse_review_resumed(value)
            out.append((topic, msg))
        return out


@pytest.fixture
def env() -> Env:
    return Env()


def test_stale_after_default_is_longer_than_judge_worst_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_STALE_AFTER", raising=False)
    assert stale_after() == timedelta(minutes=10) > timedelta(minutes=8)
    monkeypatch.setenv("REVIEW_STALE_AFTER", "900")
    assert stale_after() == timedelta(minutes=15)


async def test_received_is_republished_with_same_review(env: Env) -> None:
    await env.review("rv_recv", "received", requested_by="autofix:rv_old")

    assert await env.sweep() == [{"review_id": "rv_recv", "status": "received", "action": "requested"}]

    [(topic, msg)] = env.sent()
    assert topic == "review.requested"
    assert (msg.review_id, msg.spec_ref.commit, msg.requested_by, msg.autofix_commit) == (
        "rv_recv", HEAD, "autofix:rv_old", True)
    row = env.repo.reviews["rv_recv"]
    assert (row["status"], row["recover_count"]) == ("received", 1)
    assert await env.sweep() == []  # 가져가며 updated_at 을 새로 찍었다 — 다음 sweep 은 다시 가져가지 않는다


async def test_reviewing_goes_back_to_received_and_republished(env: Env) -> None:
    await env.review("rv_stuck", "reviewing")

    assert [r["action"] for r in await env.sweep()] == ["requested"]
    assert env.repo.reviews["rv_stuck"]["status"] == "received"
    assert [topic for topic, _ in env.sent()] == ["review.requested"]


async def test_merged_without_gitops_commit_retries_overlay(env: Env) -> None:
    await env.review("rv_merged", "merging", merge_sha=MERGE_SHA)

    assert [r["action"] for r in await env.sweep()] == ["retry_overlay"]
    [(topic, msg)] = env.sent()
    assert topic == "review.resumed" and isinstance(msg, RetryOverlayResumed) and msg.review_id == "rv_merged"
    assert env.repo.reviews["rv_merged"]["status"] == "merging"


async def test_merging_without_merge_sha_but_pr_merged_records_it_and_retries_overlay(env: Env) -> None:
    """병합 API 는 성공했는데 merge_sha 를 DB 에 쓰기 전에 죽었다."""
    await env.review("rv_m", "merging")
    env.github.pulls = [{"number": 9, "merged_at": "2026-10-09T00:00:00Z", "merge_commit_sha": "9" * 40},
                        {"number": 5, "merged_at": "2026-10-09T00:00:00Z", "merge_commit_sha": MERGE_SHA}]

    assert [r["action"] for r in await env.sweep()] == ["retry_overlay"]
    assert env.repo.reviews["rv_m"]["merge_sha"] == MERGE_SHA  # 같은 PR 번호의 병합 SHA


async def test_merging_not_merged_goes_back_to_waiting_ci_and_rechecks(env: Env) -> None:
    await env.review("rv_m", "merging")
    env.github.pulls = [{"number": 5, "state": "open", "merged_at": None, "merge_commit_sha": None}]

    assert [r["action"] for r in await env.sweep()] == ["waiting_ci"]
    assert env.repo.reviews["rv_m"]["status"] == "waiting_ci"
    [(topic, msg)] = env.sent()
    assert isinstance(msg, CiCompletedResumed) and msg.ci.head_sha == HEAD  # 워커가 check-suites 를 다시 조회한다


@pytest.mark.parametrize("status", ["needs_human", "waiting_ci", "committed", "failed", "superseded", "blocked"])
async def test_waiting_and_finished_reviews_are_not_recovered(env: Env, status: str) -> None:
    await env.review("rv_x", status, age=timedelta(days=1))

    assert await env.sweep() == []
    assert env.publisher.sent == []


async def test_fresh_reviews_are_not_recovered(env: Env) -> None:
    await env.review("rv_fresh", "reviewing", age=STALE - timedelta(minutes=1))

    assert await env.sweep() == []


async def test_more_than_max_recoveries_fails_with_verify_failure(env: Env) -> None:
    await env.review("rv_loop", "received")
    for _ in range(MAX_RECOVERIES):
        assert [r["action"] for r in await env.sweep()] == ["requested"]
        env.repo.reviews["rv_loop"]["updated_at"] -= STALE * 2  # 워커가 또 처리하지 못했다

    assert [r["action"] for r in await env.sweep()] == ["failed"]
    row = env.repo.reviews["rv_loop"]
    assert (row["status"], row["recover_count"]) == ("failed", MAX_RECOVERIES + 1)
    assert "3번" in row["error"]
    assert env.github.statuses == [(HEAD, "review-service/verify", "failure")]
    assert len(env.publisher.sent) == MAX_RECOVERIES


async def test_spec_gone_fails_instead_of_retrying(env: Env) -> None:
    env.specs.files.clear()
    await env.review("rv_nospec", "received")

    assert [r["action"] for r in await env.sweep()] == ["failed"]
    assert env.repo.reviews["rv_nospec"]["status"] == "failed"


async def test_one_broken_row_does_not_stop_others(env: Env) -> None:
    await env.review("rv_a", "merging", age=STALE * 3)  # GitHub 조회 실패
    await env.review("rv_b", "received", age=STALE * 2)

    async def boom(*_: Any) -> list[Any]:
        raise RuntimeError("github down")

    env.github.pulls_for_commit = boom  # type: ignore[method-assign]
    result = await env.sweep()

    assert [(r["review_id"], r["action"]) for r in result] == [("rv_a", "error"), ("rv_b", "requested")]
    assert env.repo.reviews["rv_a"]["status"] == "merging"  # 다음 sweep 이 다시 가져간다
