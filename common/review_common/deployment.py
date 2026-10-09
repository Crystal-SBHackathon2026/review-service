"""Dedicated post-deployment work: Kafka carries IDs, never raw errors or credentials."""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TOPIC = "deployment.analysis.requested"
MAX_ATTEMPTS = 3
MAX_EVIDENCE_VERSIONS = 3
LEASE_SECONDS = 180
log = logging.getLogger(__name__)


class AnalysisRequested(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["deployment.analysis.requested/v1"] = "deployment.analysis.requested/v1"
    event_id: str = Field(pattern=r"^de_[0-9a-f]{64}$")


async def dispatch_pending(repo, publisher) -> int:
    """Leased outbox; queued jobs are redelivered until a consumer finishes them."""
    count = 0
    for job in await repo.lease_analysis_publications():
        try:
            msg = AnalysisRequested(event_id=job["event_id"])
            await publisher.send(TOPIC, job["event_id"], msg.model_dump_json().encode())
            count += 1
        except Exception:
            # Keep durable job due for redelivery; do not log broker exceptions containing credentials.
            log.warning("deployment publication unavailable: %s", job["event_id"])
    return count


async def sweep_analysis(repo, publisher) -> None:
    while True:
        try:
            await dispatch_pending(repo, publisher)
        except Exception:
            log.warning("deployment outbox sweep unavailable")
        await asyncio.sleep(10)


async def dispatch_best_effort(repo, publisher):
    try:
        await dispatch_pending(repo, publisher)
    except Exception:
        log.warning("deployment outbox unavailable; durable jobs retained")
