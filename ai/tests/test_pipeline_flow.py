"""파이프라인이 할 일을 그대로 흉내 낸다: Review API → Kafka(JSON 왕복) → 워커 그래프 → 커밋 단계.

연결 지점 표(docs/review-ai.md)대로 쓰면 샘플 10개가 기대 결과로 끝나고,
고쳐서 통과한 명세는 applied_ops 를 원본에 적용해야 렌더링된다는 것을 고정한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from review_ai.graph import initial_state, run_graph
from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.messages import ReviewRequested, build_review_requested
from review_ai.overlay import render_overlay
from review_ai.patching import apply_ops
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import run_static_check
from tests.conftest import SAMPLES, load_sample_dict

SPEC_REF = {"repository": "github.com/crystal/sample-app", "commit": "abc123", "path": "deploy.yaml"}
SAMPLE_NAMES = sorted(p.name for p in SAMPLES.glob("[0-9]*.yaml"))
BLOCKED_AFTER_PASS = {
    "02-pass-local-sqlite.yaml": {"INGRESS_CIDRS_NOT_ENFORCED"},
    "05-fix-engine-unsupported-local.yaml": {"DB_PROVISIONING_REQUIRED", "INGRESS_CIDRS_NOT_ENFORCED"},
    "07-fix-public-bucket.yaml": {"BUCKET_PROVISIONING_REQUIRED"},
}


def _expected(name: str) -> tuple[str, bool]:
    """(최종 verdict, 고친 적 있음) — 샘플 파일 이름 규칙: pass · fix(고쳐서 통과) · human."""
    kind = Path(name).stem.split("-")[1]
    return {"pass": ("pass", False), "fix": ("pass", True), "human": ("needs_human", False)}[kind]


async def _review_like_pipeline(original: dict) -> dict:
    message = build_review_requested(original, review_id="r-1", spec_ref=SPEC_REF,
                                     requested_by="test", requested_at=datetime.now(UTC))
    message = ReviewRequested.model_validate_json(message.model_dump_json())  # Kafka 왕복
    spec = dict(message.deploy_spec)
    if "baseline" in original:
        spec["baseline"] = original["baseline"]  # 워커가 업무 DB 에서 채우는 자리
    state = initial_state(spec, review_id=message.review_id, spec_ref=message.spec_ref.model_dump())
    return await run_graph(state, llm=ScriptedLLM(oracle_review), retriever=FileRetriever())


@pytest.mark.parametrize("name", SAMPLE_NAMES)
async def test_sample_flows_through_pipeline(name: str) -> None:
    original = load_sample_dict(name)
    verdict, patched = _expected(name)

    final = await _review_like_pipeline(original)

    assert (final["status"], final["patched"]) == (verdict, patched)
    if verdict != "pass":
        return
    app_spec = {k: v for k, v in original.items() if k != "baseline"}
    fixed = apply_ops(app_spec, final["applied_ops"])
    assert not [f for f in run_static_check(DeploySpec.model_validate(fixed)) if f["severity"] != "low"]
    rendered = render_overlay(DeploySpec.model_validate(fixed))
    assert rendered.files
    # 검토를 통과해도 overlay 로 못 만드는 것(DB·버킷)은 커밋 단계가 blocked 로 멈춘다
    assert {w.code for w in rendered.blocking} == BLOCKED_AFTER_PASS.get(name, set())


async def test_fixed_spec_is_lost_if_commit_stage_reads_patch() -> None:
    """고쳐서 통과한 최종 State 는 patch=None 이다 — 커밋 단계는 applied_ops 를 써야 한다."""
    final = await _review_like_pipeline(load_sample_dict("03-fix-sqlite-replicas-gcp.yaml"))

    assert final["patch"] is None
    assert final["applied_ops"]


async def test_fixed_review_keeps_what_was_fixed_and_why() -> None:
    """고쳐서 통과하면 최종 findings·decision·retrieved_docs 는 재검사 결과(비어 있음)라, 설명은 rounds 에 남아야 한다."""
    final = await _review_like_pipeline(load_sample_dict("03-fix-sqlite-replicas-gcp.yaml"))

    assert (final["findings"], final["retrieved_docs"]) == ([], [])
    fixed_round = final["rounds"][0]
    assert [f["rule_id"] for f in fixed_round["findings"]] == ["DB-003"]
    assert fixed_round["items"] and all(item["why"] and item["cited_rule_ids"] for item in fixed_round["items"])
    assert fixed_round["doc_ids"]
