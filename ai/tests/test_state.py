from __future__ import annotations

from typing import get_args, get_type_hints

from review_ai.state import HumanDecision, ReviewStatus, Verdict


def test_review_status_is_verdict_plus_rejected() -> None:
    """status 는 verdict 값을 그대로 쓰고, 사람이 거절한 경우만 rejected 가 더해진다."""
    assert set(get_args(ReviewStatus)) == {*get_args(Verdict), "rejected"}


def test_human_decision_fields_match_review_resumed() -> None:
    """review.resumed 에 싣기로 한 값(승인/거절, 승인자, 고친 ops) — Slack 10/08 합의."""
    hints = get_type_hints(HumanDecision)
    assert set(hints) == {"decision", "approver", "edited_ops"}
    assert set(get_args(hints["decision"])) == {"approved", "rejected"}
