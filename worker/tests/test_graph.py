"""워커 그래프 — 샘플 01(pass → wait_ci), 04(baseline → needs_human), 05(fix → pass) 와 재개 경로."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from review_ai.errors import TransientError
from review_ai.judge.llm import LlmResponse
from review_ai.verdict import applied_ops
from tests.conftest import MERGE_SHA, Harness, load_sample

RID = "rv_20261008_test"


async def _seed_baseline(h: Harness, sample: dict[str, Any]) -> None:
    base = sample["baseline"]["spec"]
    await h.repo.upsert_baseline(app=base["metadata"]["name"], target_env=base["target"]["env"], spec=base,
                                 spec_ref={"repository": base["metadata"]["repository"], "commit": "b" * 40},
                                 merge_sha="b" * 40, observed_at=datetime.now(UTC))


async def _state(h: Harness) -> dict[str, Any]:
    snapshot = await h.graph.aget_state({"configurable": {"thread_id": RID}})
    return snapshot.values | {"_next": snapshot.next}


# --- 샘플 01: pass → CI 대기 → 병합 → 커밋 단계 ---------------------------------------------------

async def test_01_pass_waits_for_ci(harness: Harness) -> None:
    await harness.request(load_sample("01-pass-sample-app-aws.yaml"))

    row = harness.row()
    assert (row["status"], row["verdict"], row["reasons"]) == ("waiting_ci", "pass", [])
    assert row["final_spec"]["metadata"]["name"] == "sample-app"
    assert [f["rule_id"] for f in row["findings"]] == ["NET-001"]  # low 경고는 남는다
    assert (await _state(harness))["_next"] == ("wait_ci",)


async def test_01_ci_success_merges_then_commit_stub(harness: Harness) -> None:
    await harness.request(load_sample("01-pass-sample-app-aws.yaml"))
    await harness.ci(RID, "success")

    row = harness.row()
    assert harness.github.merged == [("Crystal-SBHackathon2026/sample-app", 7, "a" * 40)]
    assert row["merge_sha"] == MERGE_SHA
    assert row["status"] == "blocked"  # commit_overlay 는 아직 스텁
    assert row["deploy_result"]["reason"] == "COMMIT_OVERLAY_NOT_IMPLEMENTED"


async def test_ci_failure_ends_failed_without_merge(harness: Harness) -> None:
    await harness.request(load_sample("01-pass-sample-app-aws.yaml"))
    await harness.ci(RID, "failure")

    assert harness.row()["status"] == "failed"
    assert "CI failure" in harness.row()["error"]
    assert harness.github.merged == []


async def test_pr_head_moved_after_review_is_not_merged(harness: Harness) -> None:
    await harness.request(load_sample("01-pass-sample-app-aws.yaml"))
    harness.github.pulls["a" * 40] = [{"number": 7, "state": "open", "head": {"sha": "c" * 40}}]
    await harness.ci(RID, "success")

    assert harness.row()["status"] == "failed"
    assert "다르다" in harness.row()["error"]
    assert harness.github.merged == []


async def test_commit_overlay_receives_state_and_merge_sha() -> None:
    seen: dict[str, Any] = {}

    async def commit_overlay(state: dict[str, Any], merge_sha: str) -> dict[str, Any]:
        seen.update(merge_sha=merge_sha, ops=applied_ops(state))
        return {"status": "committed", "commit_sha": "d" * 40, "reason": None}

    h = Harness(commit_overlay=commit_overlay)
    await h.request(load_sample("01-pass-sample-app-aws.yaml"))
    await h.ci(RID)

    assert seen == {"merge_sha": MERGE_SHA, "ops": []}
    assert (h.row()["status"], h.row()["gitops_commit_sha"]) == ("committed", "d" * 40)


# --- 샘플 04: baseline 이 있으면 needs_human, 사람 결정으로 재개 ----------------------------------

async def test_04_baseline_from_db_needs_human(harness: Harness) -> None:
    sample = load_sample("04-human-engine-change-with-data.yaml")
    await _seed_baseline(harness, sample)
    await harness.request(sample)

    row = harness.row()
    assert (row["status"], row["verdict"]) == ("needs_human", "needs_human")
    assert "IRREVERSIBLE" in row["reasons"]
    assert "DB-001" in {f["rule_id"] for f in row["findings"]}
    assert "baseline" not in row["final_spec"]
    assert (await _state(harness))["_next"] == ("wait_human",)


async def test_04_without_baseline_is_not_engine_change(harness: Harness) -> None:
    await harness.request(load_sample("04-human-engine-change-with-data.yaml"))

    assert "DB-001" not in {f["rule_id"] for f in harness.row()["findings"] or []}


async def test_04_rejected(harness: Harness) -> None:
    sample = load_sample("04-human-engine-change-with-data.yaml")
    await _seed_baseline(harness, sample)
    await harness.request(sample)
    await harness.human(RID, "rejected")

    row = harness.row()
    assert row["status"] == "rejected"
    assert row["human_decision"]["decision"] == "rejected"
    assert (await _state(harness))["status"] == "rejected"


async def test_04_approved_without_edits_waits_for_ci(harness: Harness) -> None:
    sample = load_sample("04-human-engine-change-with-data.yaml")
    await _seed_baseline(harness, sample)
    await harness.request(sample)
    await harness.human(RID, "approved")

    assert harness.row()["status"] == "waiting_ci"
    assert harness.row()["verdict"] == "needs_human"  # AI 판정은 그대로, 사람이 승인
    assert (await _state(harness))["_next"] == ("wait_ci",)


async def test_04_approved_with_edits_rechecks_and_keeps_ops(harness: Harness) -> None:
    sample = load_sample("04-human-engine-change-with-data.yaml")
    await _seed_baseline(harness, sample)
    await harness.request(sample)
    revert = [{"op": "replace", "path": "/database/engine", "value": "postgres"},
              {"op": "replace", "path": "/database/version", "value": "16"}]
    await harness.human(RID, "approved", revert)

    state = await _state(harness)
    assert harness.row()["status"] == "waiting_ci"
    assert harness.row()["final_spec"]["database"]["engine"] == "postgres"
    assert applied_ops(state) == revert  # 커밋 단계가 원본에 적용할 ops 에 사람 편집이 들어간다


async def test_invalid_edits_go_back_to_human(harness: Harness) -> None:
    sample = load_sample("04-human-engine-change-with-data.yaml")
    await _seed_baseline(harness, sample)
    await harness.request(sample)
    await harness.human(RID, "approved", [{"op": "replace", "path": "/database/engine", "value": "oracle"}])

    row = harness.row()
    assert row["status"] == "needs_human"
    assert "edited_ops 적용 실패" in row["error"]
    assert (await _state(harness))["_next"] == ("wait_human",)


# --- 샘플 05: fix → 재검사 → pass ---------------------------------------------------------------

async def test_05_fix_then_pass(harness: Harness) -> None:
    await harness.request(load_sample("05-fix-engine-unsupported-local.yaml"))

    row = harness.row()
    assert (row["status"], row["verdict"]) == ("waiting_ci", "pass")
    assert [r["verdict"] for r in row["rounds"]] == ["fix"]
    assert row["rounds"][0]["items"]  # 무엇을 왜 고쳤는지는 rounds 에만 남는다
    assert row["final_spec"]["database"]["engine"] == "postgres"
    assert applied_ops(await _state(harness))


# --- 중복·오류 ------------------------------------------------------------------------------------

async def test_duplicate_requested_is_skipped(harness: Harness) -> None:
    raw = await harness.request(load_sample("01-pass-sample-app-aws.yaml"))
    await harness.handler.handle("review.requested", raw)

    assert harness.row()["status"] == "waiting_ci"


async def test_resume_in_wrong_status_is_skipped(harness: Harness) -> None:
    await harness.request(load_sample("01-pass-sample-app-aws.yaml"))
    await harness.human(RID, "approved")  # needs_human 이 아니므로 무시

    assert harness.row()["status"] == "waiting_ci"
    assert harness.row()["human_decision"] is None


async def test_ci_for_other_sha_is_skipped(harness: Harness) -> None:
    await harness.request(load_sample("01-pass-sample-app-aws.yaml"))
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
    await h.request(load_sample("05-fix-engine-unsupported-local.yaml"))

    assert llm.calls == 4  # 1회 + 재시도 3회
    assert (h.row()["status"], h.row()["reasons"]) == ("needs_human", ["LLM_UNAVAILABLE"])
    assert h.row()["decision"]["llm"]["error"].startswith("transient")


async def test_graph_exception_marks_failed(harness: Harness) -> None:
    async def boom(state: dict[str, Any], merge_sha: str) -> dict[str, Any]:
        raise RuntimeError("gitops push 실패")

    h = Harness(commit_overlay=boom)
    await h.request(load_sample("01-pass-sample-app-aws.yaml"))
    await h.ci(RID)

    assert h.row()["status"] == "failed"
    assert "gitops push 실패" in h.row()["error"]
