"""needs_human 뒤 사람 결정으로 재개하는 경로와, 봇이 커밋한 SHA 재검토 — Slack 10/08 합의.

- 승인 + edited_ops → apply_human_edits 가 rounds 에 남기고 명세에 적용 → static_check 부터 재검사
- 사람이 고친 뒤에 남은 문제는 AI 가 다시 고치지 않는다 → needs_human(LOOP_EXHAUSTED)
- 워커가 applied_ops 를 커밋한 SHA(autofix_commit) 를 다시 검토해 fix 가 나와도 → needs_human(LOOP_EXHAUSTED)
"""

from __future__ import annotations

from typing import Any

import pytest

from review_ai.graph import apply_human_edits, check_edited_ops, initial_state, run_graph
from review_ai.judge.fake_llm import FAKES
from review_ai.patching import PatchError, apply_ops
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.state import HumanDecision
from review_ai.verdict import applied_ops
from tests.conftest import load_sample_dict

REMOVE_TOKEN = {"op": "remove", "path": "/runtime/env/EXTERNAL_API_TOKEN"}
ADD_READINESS = {"op": "add", "path": "/runtime/health/readiness", "value": "/healthz"}


async def _review(name: str, **state_kw: Any) -> dict[str, Any]:
    state = {**initial_state(load_sample_dict(name), review_id="t"), **state_kw}
    return await run_graph(state, llm=FAKES["oracle"](), retriever=FileRetriever())


def _approved(*ops: dict[str, Any]) -> HumanDecision:
    return HumanDecision(decision="approved", approver="hyeyeon", edited_ops=list(ops))


async def _resume(paused: dict[str, Any], decision: HumanDecision) -> dict[str, Any]:
    return await run_graph({**paused, "human_decision": decision}, llm=FAKES["oracle"](), retriever=FileRetriever())


async def test_human_edits_are_rechecked_and_reach_applied_ops() -> None:
    paused = await _review("06-human-plaintext-secret.yaml")
    assert paused["status"] == "needs_human"

    final = await _resume(paused, _approved(REMOVE_TOKEN))

    assert final["status"] == "pass"
    assert final["applied_ops"] == [REMOVE_TOKEN]  # 커밋 노드가 verdict.applied_ops(state) 로 가져간다
    assert "EXTERNAL_API_TOKEN" not in final["deploy_spec"]["runtime"]["env"]


async def test_human_round_keeps_why_ai_stopped_and_who_approved() -> None:
    paused = await _review("06-human-plaintext-secret.yaml")

    final = await _resume(paused, _approved(REMOVE_TOKEN))

    [snap] = final["rounds"]
    assert (snap["verdict"], snap["reasons"]) == ("needs_human", ["AUTOFIX_FORBIDDEN"])
    assert [f["rule_id"] for f in snap["findings"]] == ["SEC-001"]
    assert snap["patch"]["ops"] == [REMOVE_TOKEN]
    assert snap["human"] == {"decision": "approved", "approver": "hyeyeon"}


async def test_leftover_after_human_edit_goes_back_to_human_not_autofix() -> None:
    paused = await _review("10-human-mixed-aws.yaml")  # RUN-001(사람) + STO-003(자동 수정 가능)

    final = await _resume(paused, _approved(ADD_READINESS))  # 사람은 RUN-001 만 고쳤다

    assert final["status"] == "needs_human"
    assert final["decision"]["reasons"] == ["LOOP_EXHAUSTED"]
    assert [f["rule_id"] for f in final["findings"]] == ["STO-003"]
    assert final["applied_ops"] == [ADD_READINESS]  # AI 패치는 적용되지 않았다
    assert final["patch"] is None


async def test_human_resume_keeps_earlier_ai_rounds_in_order() -> None:
    paused = await _review("10-human-mixed-aws.yaml")
    ai_round = {"round": 0, "finding_ids": [], "findings": [], "verdict": "fix", "reasons": [], "items": [],
                "doc_ids": [], "patch": {"kind": "config", "ops": [{"op": "replace", "path": "/runtime/replicas",
                                                                       "value": 3}],
                                         "target_finding_ids": [], "files": []}}
    paused = {**paused, "rounds": [ai_round], "retry_count": 1}

    final = await _resume(paused, _approved(ADD_READINESS))

    assert [op["path"] for op in final["applied_ops"]] == ["/runtime/replicas", "/runtime/health/readiness"]
    assert [r["round"] for r in final["rounds"]] == [0, 1]


@pytest.mark.parametrize("decision", [
    HumanDecision(decision="approved", approver="a", edited_ops=[]),
    HumanDecision(decision="rejected", approver="a", edited_ops=[REMOVE_TOKEN]),
])
async def test_apply_human_edits_only_for_approved_edits(decision: HumanDecision) -> None:
    paused = await _review("06-human-plaintext-secret.yaml")
    with pytest.raises(ValueError):
        await apply_human_edits({**paused, "human_decision": decision})


async def test_invalid_human_ops_fail_before_recheck() -> None:
    paused = await _review("06-human-plaintext-secret.yaml")
    bad = _approved({"op": "replace", "path": "/runtime/replicas", "value": "two"})
    with pytest.raises(ValueError, match="replicas"):
        await apply_human_edits({**paused, "human_decision": bad})


@pytest.mark.parametrize("path", ["/baseline/facts/database_has_data", "/baseline"])
async def test_human_cannot_edit_observed_baseline(path: str) -> None:
    """baseline 의 '데이터 없음'으로 바꾸면 DB-001(되돌릴 수 없는 엔진 변경)이 사라져 pass 가 됐다."""
    paused = await _review("04-human-engine-change-with-data.yaml")
    assert paused["status"] == "needs_human"
    op = {"op": "replace", "path": path, "value": False} if path.endswith("has_data") else {"op": "remove", "path": path}
    with pytest.raises(PatchError, match="baseline"):
        check_edited_ops(paused["deploy_spec"], [op])
    with pytest.raises(PatchError, match="baseline"):
        await apply_human_edits({**paused, "human_decision": _approved(op)})


async def test_autofix_commit_turns_fix_into_needs_human() -> None:
    """워커가 고친 ops 를 PR 브랜치에 커밋한 SHA 를 다시 검토했는데 또 fix 면, 다시 커밋하지 않고 사람에게."""
    final = await _review("03-fix-sqlite-replicas-gcp.yaml", autofix_commit=True)

    assert final["status"] == "needs_human"
    assert final["decision"]["reasons"] == ["LOOP_EXHAUSTED"]
    assert (final["rounds"], final["applied_ops"], final["patch"]) == ([], [], None)


async def test_autofix_commit_flows_from_kafka_message() -> None:
    from datetime import UTC, datetime

    from review_ai.messages import ReviewRequested, build_review_requested

    spec = load_sample_dict("03-fix-sqlite-replicas-gcp.yaml")
    sent = build_review_requested(spec, review_id="r", spec_ref={"repository": "r", "commit": "c"},
                                  requested_by="bot", requested_at=datetime.now(UTC), autofix_commit=True)
    message = ReviewRequested.model_validate_json(sent.model_dump_json())
    state = initial_state(dict(message.deploy_spec), review_id=message.review_id,
                          autofix_commit=message.autofix_commit)

    final = await run_graph(state, llm=FAKES["oracle"](), retriever=FileRetriever())

    assert final["decision"]["reasons"] == ["LOOP_EXHAUSTED"]


def test_autofix_commit_defaults_to_false_for_old_messages() -> None:
    from review_ai.messages import ReviewRequested

    fields = ReviewRequested.model_fields
    assert fields["autofix_commit"].default is False


async def test_autofix_commit_still_passes_clean_spec() -> None:
    final = await _review("01-pass-sample-app-aws.yaml", autofix_commit=True)
    assert final["status"] == "pass"


async def test_judge_node_sets_status_without_run_graph() -> None:
    """파이프라인이 make_* 로 그래프를 직접 조립해도 status 가 verdict 로 채워진다."""
    from review_ai.judge.node import make_judge

    out = await make_judge(None)({**initial_state(load_sample_dict("01-pass-sample-app-aws.yaml"), review_id="t")})
    assert out["status"] == out["decision"]["verdict"] == "pass"


def test_applied_ops_reproduces_resumed_spec() -> None:
    state = {"rounds": [{"patch": {"ops": [REMOVE_TOKEN]}}], "decision": {"verdict": "pass"}}
    original = load_sample_dict("06-human-plaintext-secret.yaml")
    assert "EXTERNAL_API_TOKEN" not in apply_ops(original, applied_ops(state))["runtime"]["env"]
