"""Kafka review.requested v1 메시지 — Review API 가 만들고 워커가 읽는다.

메시지에는 **무엇을 검토할지만** 싣는다. baseline·findings·decision·patch 는 싣지 않는다.
deploy_spec 은 mask_spec() 을 거친 뒤 싣고, spec_sha256 도 가린 값으로 계산한다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from review_ai.masking import mask_spec
from review_ai.spec.deploy_spec import PIPELINE_FIELDS, AppSpec, user_fields

SCHEMA_VERSION = "review.requested/v1"
TOPIC = "review.requested"


class SpecRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    commit: str
    path: str = "deploy.yaml"


class ReviewRequested(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["review.requested/v1"] = SCHEMA_VERSION
    review_id: str = Field(min_length=1, description="멱등 키 + LangGraph thread_id")
    repo_id: str = Field(min_length=1, description="Kafka 메시지 키 — 같은 레포는 순서대로 처리")
    app: str
    target_env: Literal["aws", "gcp", "local"]
    spec_ref: SpecRef
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deploy_spec: dict[str, Any] = Field(description="mask_spec() 을 거친 명세. baseline 없음")
    requested_by: str
    requested_at: datetime
    autofix_commit: bool = Field(
        default=False,
        description="spec_ref.commit 이 워커가 applied_ops 를 커밋한 것(봇 커밋)이면 True — 또 fix 면 needs_human",
    )

    @model_validator(mode="after")
    def _safe_payload(self) -> ReviewRequested:
        if pipeline := [k for k in PIPELINE_FIELDS if k in self.deploy_spec]:
            raise ValueError(f"{', '.join(pipeline)} 은 메시지에 싣지 않는다 — 워커가 처리 시점에 채운다")
        if mask_spec(self.deploy_spec) != self.deploy_spec:
            raise ValueError("deploy_spec 에 가리지 않은 비밀 값이 있다 — mask_spec() 을 거쳐야 한다")
        if spec_sha256(self.deploy_spec) != self.spec_sha256:
            raise ValueError("spec_sha256 이 deploy_spec 과 맞지 않다")
        AppSpec.model_validate(self.deploy_spec)
        return self


def spec_sha256(spec: dict[str, Any]) -> str:
    canonical = json.dumps(spec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_review_requested(
    spec: dict[str, Any],
    *,
    review_id: str,
    spec_ref: dict[str, str],
    requested_by: str,
    requested_at: datetime,
    autofix_commit: bool = False,
) -> ReviewRequested:
    body = mask_spec(user_fields(spec))
    return ReviewRequested(
        review_id=review_id,
        repo_id=body["metadata"]["repository"],
        app=body["metadata"]["name"],
        target_env=body["target"]["env"],
        spec_ref=SpecRef(**spec_ref),
        spec_sha256=spec_sha256(body),
        deploy_spec=body,
        requested_by=requested_by,
        requested_at=requested_at,
        autofix_commit=autofix_commit,
    )
