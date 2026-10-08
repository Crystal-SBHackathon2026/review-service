from __future__ import annotations

from typing import NotRequired, get_args, get_origin, get_type_hints

from review_ai.state import HumanDecision, PatchOp, ReviewStatus, Verdict


def test_review_status_is_verdict_plus_running_and_rejected() -> None:
    """status 는 verdict 값을 그대로 쓰고, 판정 전 running·사람이 거절한 rejected 만 더해진다."""
    assert set(get_args(ReviewStatus)) == {"running", *get_args(Verdict), "rejected"}


def test_human_decision_required_input_and_optional_defaults() -> None:
    """기존 필수 입력 3개 유지. 권장값 선택과 서버 보충 기록은 선택 필드다."""
    hints = get_type_hints(HumanDecision, include_extras=True)
    optional = {name for name, hint in hints.items() if get_origin(hint) is NotRequired}
    assert HumanDecision.__total__ is True
    assert set(hints) - optional == {"decision", "approver", "edited_ops"}
    assert optional == {"use_recommendations", "defaulted_ops"}
    assert set(get_args(hints["decision"])) == {"approved", "rejected"}
    assert get_args(hints["use_recommendations"]) == (bool,)
    assert get_args(hints["defaulted_ops"]) == (list[PatchOp],)
