"""워커 그래프 — 샘플 01(pass → CI), 04(baseline → needs_human), 05(fix → AI 수정 커밋 → 재검토 → pass) 와 재개 경로."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import yaml

from review_ai.errors import TransientError
from review_ai.judge.llm import LlmResponse
from review_ai.messages import ReviewRequested
from review_ai.verdict import applied_ops
from review_common.github import GitHubError, RefConflict
from tests.conftest import FIX_SHA, HEAD, MERGE_SHA, Harness, load_sample, suite

RID = "rv_20261008_test"
SAMPLE_01 = "01-pass-sample-app-aws.yaml"
SAMPLE_04 = "04-human-engine-change-with-data.yaml"
SAMPLE_05 = "05-fix-engine-unsupported-local.yaml"


async def _seed_baseline(h: Harness, sample: dict[str, Any]) -> None:
    base = sample["baseline"]["spec"]
    await h.repo.upsert_baseline(app=base["metadata"]["name"], target_env=base["target"]["env"], spec=base,
                                 spec_ref={"repository": base["metadata"]["repository"], "commit": "c" * 40},
                                 merge_sha="c" * 40, observed_at=datetime.now(UTC))


async def _state(h: Harness, review_id: str = RID) -> dict[str, Any]:
    snapshot = await h.graph.aget_state({"configurable": {"thread_id": review_id}})
    return snapshot.values | {"_next": snapshot.next}


async def _finish_ci(h: Harness, review_id: str = RID, conclusion: str = "success", sha: str = HEAD) -> None:
    """CI 가 끝나고 check_suite 웹훅이 온 상황 — GitHub 에도 끝난 suite 가 보인다."""
    h.github.suites[sha] = [suite(conclusion=conclusion)]
    await h.ci(review_id, conclusion, head_sha=sha)


# --- 샘플 01: 수정 없이 pass → CI → 병합 → 커밋 단계 --------------------------------------------

async def test_01_pass_waits_for_ci(harness: Harness) -> None:
    await harness.request(load_sample(SAMPLE_01))

    row = harness.row()
    assert (row["status"], row["verdict"], row["reasons"]) == ("waiting_ci", "pass", [])
    assert row["final_spec"]["metadata"]["name"] == "sample-app"
    assert [f["rule_id"] for f in row["findings"]] == ["NET-001"]  # low 경고는 남는다
    assert (await _state(harness))["_next"] == ("wait_ci",)
    assert harness.github.commits == []  # 고칠 것이 없으면 커밋하지 않는다


async def test_01_ci_already_finished_does_not_wait(harness: Harness) -> None:
    """gitops#9 — 검토가 CI 보다 늦게 끝나면 check_suite 웹훅은 이미 지나갔다. 기다리지 않고 바로 병합한다."""
    harness.github.suites[HEAD] = [suite(conclusion="success")]
    await harness.request(load_sample(SAMPLE_01))

    assert harness.github.merged == [("Crystal-SBHackathon2026/sample-app", 7, HEAD)]
    assert (harness.row()["status"], harness.row()["merge_sha"]) == ("blocked", MERGE_SHA)
    assert (await _state(harness))["_next"] == ()


async def test_01_ci_already_failed_ends_failed(harness: Harness) -> None:
    harness.github.suites[HEAD] = [suite(conclusion="failure")]
    await harness.request(load_sample(SAMPLE_01))

    assert harness.row()["status"] == "failed"
    assert "CI failure" in harness.row()["error"]
    assert harness.github.merged == []


async def test_ci_still_running_waits(harness: Harness) -> None:
    harness.github.suites[HEAD] = [suite(status="in_progress", conclusion=None)]
    await harness.request(load_sample(SAMPLE_01))

    assert harness.row()["status"] == "waiting_ci"
    assert (await _state(harness))["_next"] == ("wait_ci",)


async def test_other_apps_suites_are_ignored(harness: Harness) -> None:
    """다른 GitHub App 의 suite(끝나지 않은 채 남는 경우가 있다)는 보지 않는다."""
    harness.github.suites[HEAD] = [suite(conclusion="success"), suite(status="queued", conclusion=None, slug="other")]
    await harness.request(load_sample(SAMPLE_01))

    assert harness.github.merged


async def test_check_suites_error_falls_back_to_webhook(harness: Harness) -> None:
    harness.github.suite_error = GitHubError("boom", 502)
    await harness.request(load_sample(SAMPLE_01))
    assert harness.row()["status"] == "waiting_ci"

    await harness.ci(RID, "success")  # 조회가 여전히 실패하면 웹훅 결론을 쓴다
    assert harness.github.merged


async def test_01_ci_success_merges_then_commit_stub(harness: Harness) -> None:
    await harness.request(load_sample(SAMPLE_01))
    await _finish_ci(harness)

    row = harness.row()
    assert harness.github.merged == [("Crystal-SBHackathon2026/sample-app", 7, HEAD)]
    assert row["merge_sha"] == MERGE_SHA
    assert row["status"] == "blocked"  # commit_overlay 는 아직 스텁
    assert row["deploy_result"]["reason"] == "COMMIT_OVERLAY_NOT_IMPLEMENTED"


async def test_webhook_while_another_suite_runs_keeps_waiting(harness: Harness) -> None:
    """workflow 가 여러 개면 첫 suite 완료 웹훅에 병합하지 않는다 — 전부 끝날 때까지 다시 기다린다."""
    await harness.request(load_sample(SAMPLE_01))
    harness.github.suites[HEAD] = [suite(conclusion="success"), suite(status="in_progress", conclusion=None)]
    await harness.ci(RID, "success")

    assert harness.row()["status"] == "waiting_ci"
    assert harness.github.merged == []
    assert (await _state(harness))["_next"] == ("wait_ci",)

    await _finish_ci(harness)
    assert harness.github.merged


async def test_ci_failure_ends_failed_without_merge(harness: Harness) -> None:
    await harness.request(load_sample(SAMPLE_01))
    await _finish_ci(harness, conclusion="failure")

    assert harness.row()["status"] == "failed"
    assert "CI failure" in harness.row()["error"]
    assert harness.github.merged == []


async def test_pr_head_moved_after_review_is_not_merged(harness: Harness) -> None:
    await harness.request(load_sample(SAMPLE_01))
    harness.github.pulls[HEAD][0]["head"]["sha"] = "c" * 40
    await _finish_ci(harness)

    assert harness.row()["status"] == "failed"
    assert "다르다" in harness.row()["error"]
    assert harness.github.merged == []


async def test_commit_overlay_receives_state_and_merge_sha() -> None:
    seen: dict[str, Any] = {}

    async def commit_overlay(state: dict[str, Any]) -> dict[str, Any]:
        seen.update(ref=state["spec_ref"]["commit"], ops=applied_ops(state))
        return {"deploy_result": {"status": "committed", "commit_sha": "d" * 40, "reason": None}}

    h = Harness(commit_overlay=commit_overlay)
    await h.request(load_sample(SAMPLE_01))
    await _finish_ci(h)

    assert seen == {"ref": HEAD, "ops": []}
    assert h.row()["merge_sha"] == MERGE_SHA  # 병합 SHA 는 merge_pr 이 DB 에
    assert (h.row()["status"], h.row()["gitops_commit_sha"]) == ("committed", "d" * 40)


# --- 샘플 05: fix → AI 수정 커밋 → autofix_commit 재검토 → pass ----------------------------------

async def test_05_fix_commits_to_pr_branch_and_rereviews(harness: Harness) -> None:
    await harness.request(load_sample(SAMPLE_05))

    row = harness.row()
    assert (row["status"], row["verdict"]) == ("superseded", "pass")
    assert [r["verdict"] for r in row["rounds"]] == ["fix"]
    assert row["rounds"][0]["items"]  # 무엇을 왜 고쳤는지는 rounds 에 남는다
    [commit] = harness.github.commits
    assert (commit["branch"], commit["parent"]) == ("feature", HEAD)
    committed = yaml.safe_load(commit["content"])
    assert committed["database"]["engine"] == "postgres"
    assert committed["secrets"] == load_sample(SAMPLE_05)["secrets"]  # 원본 값 그대로 (가린 값이 아님)

    [(topic, key, value)] = harness.publisher.sent
    msg = ReviewRequested.model_validate_json(value)
    assert (topic, key, msg.autofix_commit, msg.spec_ref.commit) == (
        "review.requested", "Crystal-SBHackathon2026/sample-orders", True, FIX_SHA)
    assert row["superseded_by"] == msg.review_id
    new = harness.row(msg.review_id)
    assert (new["status"], new["pr_head_sha"], new["requested_by"]) == ("received", FIX_SHA, f"autofix:{RID}")
    assert harness.github.prepared == {}  # 커밋을 만들고 → DB 에 넣고 → 브랜치를 옮겼다

    await harness.deliver_published()  # 수정 커밋을 다시 검토 — 이번엔 고칠 것이 없다
    assert (harness.row(msg.review_id)["status"], harness.row(msg.review_id)["verdict"]) == ("waiting_ci", "pass")

    await _finish_ci(harness, msg.review_id, sha=FIX_SHA)  # 새 커밋이라 CI 가 다시 돈다
    assert harness.github.merged == [("Crystal-SBHackathon2026/sample-orders", 7, FIX_SHA)]


async def test_autofix_commit_review_that_needs_fix_again_goes_to_human(harness: Harness) -> None:
    """봇 커밋을 다시 검토했는데 또 fix — 다시 커밋하지 않고 needs_human(LOOP_EXHAUSTED)."""
    from review_ai.messages import build_review_requested

    spec = load_sample(SAMPLE_05)
    ref = {"repository": spec["metadata"]["repository"], "commit": HEAD, "path": "deploy.yaml"}
    msg = build_review_requested(spec, review_id=RID, spec_ref=ref, requested_by="autofix:x",
                                 requested_at=datetime.now(UTC), autofix_commit=True)
    await harness.repo.insert_review(review_id=RID, app=msg.app, target_env=msg.target_env, repo_id=msg.repo_id,
                                     spec_ref=ref, pr_head_sha=HEAD, requested_by="autofix:x")
    await harness.handler.handle("review.requested", msg.model_dump_json().encode())

    assert (harness.row()["status"], harness.row()["reasons"]) == ("needs_human", ["LOOP_EXHAUSTED"])
    assert harness.github.commits == []


async def test_fork_pr_cannot_be_autofixed(harness: Harness) -> None:
    harness.github.open_pr("Crystal-SBHackathon2026/sample-orders", HEAD, fork=True)
    await harness.request(load_sample(SAMPLE_05))

    assert harness.row()["status"] == "failed"
    assert "포크" in harness.row()["error"]
    assert harness.github.commits == [] and harness.publisher.sent == []


async def test_branch_moved_before_fix_commit_fails(harness: Harness) -> None:
    """수정 커밋을 만든 사이 사람이 푸시했다 — 브랜치를 못 옮기면 수정을 버리고 새 검토도 failed."""
    harness.github.branch_error = RefConflict("feature 가 그사이 움직였다", 422)
    await harness.request(load_sample(SAMPLE_05))

    row = harness.row()
    assert row["status"] == "failed" and "움직였다" in row["error"]
    [new] = [r for r in harness.repo.reviews.values() if r["review_id"] != RID]
    assert (new["status"], new["pr_head_sha"]) == ("failed", FIX_SHA)
    assert harness.publisher.sent == [] and harness.github.commits == []


# --- 샘플 04: baseline 이 있으면 needs_human, 사람 결정으로 재개 ----------------------------------

async def test_04_baseline_from_db_needs_human(harness: Harness) -> None:
    sample = load_sample(SAMPLE_04)
    await _seed_baseline(harness, sample)
    await harness.request(sample)

    row = harness.row()
    assert (row["status"], row["verdict"]) == ("needs_human", "needs_human")
    assert "IRREVERSIBLE" in row["reasons"]
    assert "DB-001" in {f["rule_id"] for f in row["findings"]}
    assert "baseline" not in row["final_spec"]
    assert (await _state(harness))["_next"] == ("wait_human",)


async def test_04_without_baseline_is_not_engine_change(harness: Harness) -> None:
    await harness.request(load_sample(SAMPLE_04))

    assert "DB-001" not in {f["rule_id"] for f in harness.row()["findings"] or []}


async def test_04_rejected(harness: Harness) -> None:
    sample = load_sample(SAMPLE_04)
    await _seed_baseline(harness, sample)
    await harness.request(sample)
    await harness.human(RID, "rejected")

    row = harness.row()
    assert row["status"] == "rejected"
    assert row["human_decision"]["decision"] == "rejected"
    assert (await _state(harness))["status"] == "rejected"


async def test_04_approved_without_edits_waits_for_ci(harness: Harness) -> None:
    sample = load_sample(SAMPLE_04)
    await _seed_baseline(harness, sample)
    await harness.request(sample)
    await harness.human(RID, "approved")

    assert harness.row()["status"] == "waiting_ci"
    assert harness.row()["verdict"] == "needs_human"  # AI 판정은 그대로, 사람이 승인
    assert harness.github.commits == []
    assert (await _state(harness))["_next"] == ("wait_ci",)


async def test_04_approved_with_edits_rechecks_and_commits(harness: Harness) -> None:
    """사람 수정도 앱 레포에 커밋한다 — apply_human_edits 가 rounds 에 남겨 applied_ops 에 들어간다."""
    sample = load_sample(SAMPLE_04)
    await _seed_baseline(harness, sample)
    await harness.request(sample)
    revert = [{"op": "replace", "path": "/database/engine", "value": "postgres"},
              {"op": "replace", "path": "/database/version", "value": "16"}]
    await harness.human(RID, "approved", revert)

    state = await _state(harness)
    assert applied_ops(state) == revert
    assert state["rounds"][-1]["human"] == {"decision": "approved", "approver": "hyeyeon"}
    assert harness.row()["status"] == "superseded"
    assert yaml.safe_load(harness.github.commits[0]["content"])["database"]["engine"] == "postgres"


async def test_invalid_edits_go_back_to_human(harness: Harness) -> None:
    sample = load_sample(SAMPLE_04)
    await _seed_baseline(harness, sample)
    await harness.request(sample)
    await harness.human(RID, "approved", [{"op": "replace", "path": "/database/engine", "value": "oracle"}])

    row = harness.row()
    assert row["status"] == "needs_human"
    assert "edited_ops 적용 실패" in row["error"]
    assert (await _state(harness))["_next"] == ("wait_human",)


# --- 중복·오류 ------------------------------------------------------------------------------------

async def test_duplicate_requested_is_skipped(harness: Harness) -> None:
    raw = await harness.request(load_sample(SAMPLE_01))
    await harness.handler.handle("review.requested", raw)

    assert harness.row()["status"] == "waiting_ci"


async def test_resume_in_wrong_status_is_skipped(harness: Harness) -> None:
    await harness.request(load_sample(SAMPLE_01))
    await harness.human(RID, "approved")  # needs_human 이 아니므로 무시

    assert harness.row()["status"] == "waiting_ci"
    assert harness.row()["human_decision"] is None


async def test_ci_for_other_sha_is_skipped(harness: Harness) -> None:
    await harness.request(load_sample(SAMPLE_01))
    await harness.ci(RID, "success", head_sha="e" * 40)

    assert harness.row()["status"] == "waiting_ci"
    assert harness.github.merged == []


async def test_malformed_message_is_skipped(harness: Harness) -> None:
    await harness.handler.handle("review.requested", b'{"not": "a review"}')
    await harness.handler.handle("review.resumed", b"not json")


class AlwaysTransient:
    model = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: Any) -> LlmResponse:
        self.calls += 1
        raise TransientError("529")


async def test_judge_transient_retries_then_llm_unavailable() -> None:
    llm = AlwaysTransient()
    h = Harness(llm=llm)
    await h.request(load_sample(SAMPLE_05))

    assert llm.calls == 4  # 1회 + 재시도 3회
    assert (h.row()["status"], h.row()["reasons"]) == ("needs_human", ["LLM_UNAVAILABLE"])
    assert h.row()["decision"]["llm"]["error"].startswith("transient")


async def test_graph_exception_marks_failed() -> None:
    async def boom(state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("gitops push 실패")

    h = Harness(commit_overlay=boom)
    await h.request(load_sample(SAMPLE_01))
    await _finish_ci(h)

    assert h.row()["status"] == "failed"
    assert "gitops push 실패" in h.row()["error"]


# --- commit_overlay 연결 (성진님 make_commit_overlay + GitClient) ------------------------------------

class FakeGitClient:
    """GitClient — 앱 레포는 FakeGitHub 파일, gitops 커밋은 메모리. 처음 fail_times 번은 TransientError."""

    def __init__(self, github: Any, fail_times: int = 0) -> None:
        self.github, self.fail_times = github, fail_times
        self.commits: list[tuple[str, dict[str, str], str]] = []

    async def read_file(self, repository: str, path: str, ref: str) -> str:
        return self.github.files[ref]

    async def commit_files(self, directory: str, files: Any, message: str) -> str:
        if self.fail_times:
            self.fail_times -= 1
            raise TransientError("gitops ref 충돌")
        self.commits.append((directory, dict(files), message))
        return "e" * 40


def _with_git(fail_times: int = 0) -> tuple[Harness, FakeGitClient]:
    from review_worker.commit_overlay import make_commit_overlay

    h = Harness()
    git = FakeGitClient(h.github, fail_times)
    h.deps.commit_overlay = make_commit_overlay(git)
    return h, git


async def test_merge_then_commit_overlay_committed() -> None:
    h, git = _with_git()
    await h.request(load_sample(SAMPLE_01))
    await _finish_ci(h)

    row = h.row()
    assert (row["status"], row["gitops_commit_sha"], row["merge_sha"]) == ("committed", "e" * 40, MERGE_SHA)
    [(directory, files, message)] = git.commits
    assert directory == "apps/sample-app/overlays/aws"
    assert set(files) == {"ingress.yaml", "kustomization.yaml"}
    assert message.startswith("chore: sample-app aws overlay 갱신")


async def test_commit_overlay_blocking_warning_is_blocked() -> None:
    """검토는 통과(baseline 없는 04)했지만 overlay 로 관리형 DB 를 못 만든다 → 커밋하지 않고 blocked."""
    h, git = _with_git()
    await h.request(load_sample(SAMPLE_04))
    await _finish_ci(h)

    row = h.row()
    assert (row["status"], row["gitops_commit_sha"]) == ("blocked", None)
    assert "DB_PROVISIONING_REQUIRED" in row["deploy_result"]["reason"]
    assert git.commits == []


async def test_commit_overlay_retries_transient_error() -> None:
    h, git = _with_git(fail_times=1)
    await h.request(load_sample(SAMPLE_01))
    await _finish_ci(h)

    assert h.row()["status"] == "committed"
    assert len(git.commits) == 1


async def test_approval_after_ai_patch_round_commits_fix(harness: Harness) -> None:
    """AI 가 한 번 고친 뒤 needs_human 이 됐는데 사람이 그대로 승인 — applied_ops 가 있으니 앱 레포에도 커밋한다."""
    spec = load_sample(SAMPLE_05)
    await harness.request(spec)  # 05: fix → 수정 커밋 → superseded. 같은 스레드를 needs_human 에 멈춘 상태로 다시 만든다
    config = {"configurable": {"thread_id": "rv_paused"}}
    rounds = (await _state(harness))["rounds"]
    ref = {"repository": spec["metadata"]["repository"], "commit": HEAD, "path": "deploy.yaml"}
    await harness.repo.insert_review(review_id="rv_paused", app="orders", target_env="local",
                                     repo_id=ref["repository"], spec_ref=ref, pr_head_sha=HEAD, requested_by="t")
    await harness.repo.update_review("rv_paused", status="needs_human")
    harness.github.open_pr(ref["repository"], HEAD)
    harness.github.commits.clear()
    harness.publisher.sent.clear()
    await harness.graph.aupdate_state(config, {
        "review_id": "rv_paused", "target_env": "local", "spec_ref": ref, "deploy_spec": spec, "rounds": rounds,
        "findings": [], "retrieved_docs": [], "patch": None, "retry_count": 1,
        "decision": {"verdict": "needs_human", "reasons": ["LLM_UNAVAILABLE"], "items": [], "extra_opinions": [],
                     "validation": {}, "llm": None},
    }, as_node="await_human")
    await harness.graph.ainvoke(None, config)  # wait_human 에서 멈춘다
    await harness.human("rv_paused", "approved")

    assert harness.row("rv_paused")["status"] == "superseded"
    assert yaml.safe_load(harness.github.commits[0]["content"])["database"]["engine"] == "postgres"
    assert ReviewRequested.model_validate_json(harness.publisher.sent[0][2]).autofix_commit
