"""review_id 발급 — rv_YYYYMMDD_<8hex>. Review API 와 워커(AI 수정 커밋 재검토)가 같이 쓴다."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime


def new_review_id(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"rv_{now:%Y%m%d}_{secrets.token_hex(4)}"
