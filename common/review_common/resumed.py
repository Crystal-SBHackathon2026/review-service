"""Kafka review.resumed v1 메시지 — Review API(사람 결정·CI 웹훅)가 만들고 워커가 읽어 interrupt 를 재개한다.

    {"schema_version": "review.resumed/v1", "review_id": "...", "kind": "human_decision",
     "human_decision": {"decision": "approved", "approver": "...", "edited_ops": []}, "resumed_at": "..."}
    {"schema_version": "review.resumed/v1", "review_id": "...", "kind": "ci_completed",
     "ci": {"head_sha": "...", "conclusion": "success"}, "resumed_at": "..."}

메시지 키는 review_id.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

TOPIC = "review.resumed"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PatchOpModel(_Frozen):
    """deploy_spec JSON Patch 한 줄. review_ai.state.PatchOp 와 같은 모양."""

    op: Literal["replace", "add", "remove"]
    path: str = Field(pattern=r"^/")
    value: Any = None

    def as_op(self) -> dict[str, Any]:
        return self.model_dump(exclude={"value"}) if self.op == "remove" else self.model_dump()


class HumanDecisionModel(_Frozen):
    decision: Literal["approved", "rejected"]
    approver: str = Field(min_length=1)
    edited_ops: list[PatchOpModel] = []

    def as_state(self) -> dict[str, Any]:
        """review_ai.state.HumanDecision 모양."""
        return {"decision": self.decision, "approver": self.approver,
                "edited_ops": [op.as_op() for op in self.edited_ops]}


class CiResult(_Frozen):
    head_sha: str = Field(min_length=1)
    conclusion: str = Field(min_length=1)  # GitHub check_suite conclusion: success·failure·cancelled 등


class HumanDecisionResumed(_Frozen):
    schema_version: Literal["review.resumed/v1"] = "review.resumed/v1"
    review_id: str = Field(min_length=1)
    kind: Literal["human_decision"] = "human_decision"
    human_decision: HumanDecisionModel
    resumed_at: datetime


class CiCompletedResumed(_Frozen):
    schema_version: Literal["review.resumed/v1"] = "review.resumed/v1"
    review_id: str = Field(min_length=1)
    kind: Literal["ci_completed"] = "ci_completed"
    ci: CiResult
    resumed_at: datetime


ReviewResumed = Annotated[HumanDecisionResumed | CiCompletedResumed, Field(discriminator="kind")]
REVIEW_RESUMED = TypeAdapter(ReviewResumed)


def parse_review_resumed(raw: bytes | str) -> HumanDecisionResumed | CiCompletedResumed:
    return REVIEW_RESUMED.validate_json(raw)
