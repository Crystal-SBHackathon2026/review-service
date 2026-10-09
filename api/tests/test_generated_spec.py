"""intake 가 baseline 없이 만든 명세(missing·empty)가 든 PR — 검토 요청에 generated_spec 을 싣는다 (pass 여도 needs_human).

10/09 sample-app #11: 레포 분석만으로 만든 명세(network: {})가 pass → 자동 병합 → gitops ingress 삭제 → ALB 삭제.
판단 근거는 spec_intakes 기록(PR 번호·kind·생성 커밋·baseline_used)뿐이다. intake 가 어떤 명세를 만들지(만들지 않을지)와
상관없이 이 층을 확인하려고 대부분 intake 행을 직접 넣는다.
"""

from __future__ import annotations

from typing import Any

import yaml

from review_ai.messages import ReviewRequested
from tests.test_api import HEAD, REPO, pr_event, sample_text, send_pr
from tests.test_intake import BROKEN, SAMPLE_TREE, IntakeEnv, ienv, repairing_llm  # noqa: F401 — ienv 는 fixture

GENERATED = "9" * 40


def _generated_text() -> str:
    spec = yaml.safe_load(sample_text())
    spec["network"] = {}  # 장애 때처럼 공개 범위를 모르는 채로 만든 명세
    return yaml.safe_dump(spec, sort_keys=False)


async def _seed(env: IntakeEnv, *, baseline_used: bool | None, kind: str = "missing") -> str:
    """intake 가 PR #5 head 에 생성 커밋(GENERATED)을 올린 상태 — 행과 커밋 파일."""
    intake_id = "in_gen"
    await env.repo.insert_intake(intake_id=intake_id, repository=REPO, head_repository=REPO, pr_number=5,
                                 head_sha=HEAD, head_ref="feature", path="deploy.yaml", kind=kind, errors=[],
                                 requested_by="octo-dev")
    await env.repo.link_intake(intake_id, result_commit_sha=GENERATED, baseline_used=baseline_used)
    status, reason = ("generated", "GENERATED") if kind in ("missing", "empty") else ("repaired", "REPAIRED")
    await env.repo.finish_intake(intake_id, status=status, reason=reason, message="m", result_commit_sha=GENERATED)
    env.put(_generated_text(), sha=GENERATED)
    return intake_id


def _review(env: IntakeEnv, sha: str, number: int = 5) -> tuple[dict[str, Any], ReviewRequested]:
    env.publisher.sent.clear()
    body = send_pr(env, pr_event("synchronize", sha=sha, number=number)).json()
    [(_, _, value)] = env.publisher.sent
    return body, ReviewRequested.model_validate_json(value)


async def test_generated_commit_without_baseline_is_sent_as_generated_spec(ienv: IntakeEnv) -> None:
    intake_id = await _seed(ienv, baseline_used=False)
    body, msg = _review(ienv, GENERATED)

    assert (msg.generated_spec, body["generated_spec"], body["from_intake"]) == (True, intake_id, intake_id)


async def test_generated_commit_from_baseline_is_a_plain_review(ienv: IntakeEnv) -> None:
    await _seed(ienv, baseline_used=True)
    body, msg = _review(ienv, GENERATED)

    assert msg.generated_spec is False and "generated_spec" not in body


async def test_unknown_baseline_use_is_treated_as_unverified(ienv: IntakeEnv) -> None:
    """0006 전에 이어진 행처럼 baseline_used 가 비었으면 모르는 것 — 사람에게."""
    await _seed(ienv, baseline_used=None)
    assert _review(ienv, GENERATED)[1].generated_spec is True


async def test_empty_spec_generation_counts_too(ienv: IntakeEnv) -> None:
    await _seed(ienv, baseline_used=False, kind="empty")
    assert _review(ienv, GENERATED)[1].generated_spec is True


async def test_repaired_spec_is_a_plain_review(ienv: IntakeEnv) -> None:
    """형식 오류 복구는 값을 원문과 대조한다 — baseline 이 없어도 범위 밖."""
    await _seed(ienv, baseline_used=False, kind="yaml_error")
    assert _review(ienv, GENERATED)[1].generated_spec is False


async def test_later_commit_on_the_generated_pr_is_still_generated_spec(ienv: IntakeEnv) -> None:
    """생성 커밋 위에 코드만 바꾼 커밋 — 명세는 여전히 레포 분석으로 만든 것이다. 다른 PR 은 상관없다."""
    await _seed(ienv, baseline_used=False)
    later = "e" * 40
    ienv.put(_generated_text(), sha=later)

    assert _review(ienv, later)[1].generated_spec is True
    other = "d" * 40
    ienv.put(_generated_text(), sha=other)
    assert _review(ienv, other, number=99)[1].generated_spec is False


async def test_intake_records_baseline_used_when_generating_from_baseline(ienv: IntakeEnv) -> None:
    await ienv.approve_baseline()
    send_pr(ienv, pr_event("opened"))

    row = ienv.only_intake()
    assert (row["status"], row["baseline_used"]) == ("generated", True)
    assert ienv.client.get(f"/intakes/{row['intake_id']}").json()["baseline_used"] is True
    ienv.put(ienv.github.contents[row["result_commit_sha"]], sha=row["result_commit_sha"])
    assert _review(ienv, row["result_commit_sha"])[1].generated_spec is False


async def test_intake_records_baseline_unused_when_repairing_new_app() -> None:
    env = IntakeEnv(repair_llm=repairing_llm())
    env.github.tree = dict(SAMPLE_TREE)
    env.put(BROKEN)
    send_pr(env, pr_event("opened"))

    row = env.only_intake()
    assert (row["status"], row["baseline_used"]) == ("repaired", False)


async def test_review_detail_explains_generated_spec_reason(ienv: IntakeEnv) -> None:
    ienv.put(sample_text())
    review_id = send_pr(ienv, pr_event("opened")).json()["review_id"]
    await ienv.repo.update_review(review_id, status="needs_human", verdict="needs_human",
                                  reasons=["GENERATED_SPEC_UNVERIFIED"])

    detail = ienv.client.get(f"/reviews/{review_id}").json()
    assert detail["reasons"] == ["GENERATED_SPEC_UNVERIFIED"]
    assert detail["reason_messages"] == {
        "GENERATED_SPEC_UNVERIFIED": "baseline 없이 생성된 명세 — 공개 범위(network·ingress)·env·replicas 확인 필요"}
