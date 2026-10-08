"""ID 발급 — review_id rv_YYYYMMDD_<8hex> (Review API·워커가 같이 쓴다), intake_id in_YYYYMMDD_<8hex>."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime


def new_review_id(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"rv_{now:%Y%m%d}_{secrets.token_hex(4)}"


def new_intake_id(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"in_{now:%Y%m%d}_{secrets.token_hex(4)}"
