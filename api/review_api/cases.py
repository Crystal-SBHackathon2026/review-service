"""Argo CD Degraded → 판단 사례(review_cases). 다음 검토가 같은 규칙에 걸리면 '지난번 승인했지만 배포 실패'가 근거로 붙는다.

병합된 검토가 AI 수정 커밋의 재검토(requested_by=autofix:<원래 검토>)면 판단(finding·사람 응답·수정)은 원래 검토에
있으므로 거슬러 올라가 그 검토로 사례를 만든다.
"""

from __future__ import annotations

import logging
from typing import Any

from review_ai.cases import case_from_review
from review_common.repository import ReviewRepository

AUTOFIX_PREFIX = "autofix:"
MAX_AUTOFIX_HOPS = 3  # 재검토에서 또 고치면 LOOP_EXHAUSTED 로 막히므로 실제로는 한 번이면 된다
log = logging.getLogger(__name__)


async def record_degraded_case(repo: ReviewRepository, row: dict[str, Any]) -> str | None:
    """사례를 남긴 검토 ID. 남길 판단이 없거나 기록에 실패하면 None — 배포 이벤트 기록은 그대로 둔다."""
    current: dict[str, Any] | None = row
    try:
        for _ in range(MAX_AUTOFIX_HOPS + 1):
            if current is None:
                return None
            case = case_from_review(current, "deploy_degraded")
            if case is not None:
                await repo.insert_case(**case)
                return current["review_id"]
            requested_by = current.get("requested_by") or ""
            if not requested_by.startswith(AUTOFIX_PREFIX):
                return None
            current = await repo.get_review(requested_by.removeprefix(AUTOFIX_PREFIX))
    except Exception:
        log.warning("review %s: Degraded 사례 기록 실패", row["review_id"], exc_info=True)
    return None
