"""Versioned Argo CD contract. The original five fields remain accepted."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from review_ai.failure_evidence import safe_data

MAX_BODY = 256 * 1024


class Health(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str | None = Field(default=None, max_length=256)
    message: str | None = Field(default=None, max_length=8192)


class Resource(BaseModel):
    model_config = ConfigDict(extra="ignore")
    group: str | None = Field(default=None, max_length=256)
    version: str | None = Field(default=None, max_length=256)
    kind: str | None = Field(default=None, max_length=256)
    namespace: str | None = Field(default=None, max_length=256)
    name: str | None = Field(default=None, max_length=256)
    status: str | None = Field(default=None, max_length=256)
    message: str | None = Field(default=None, max_length=8192)
    health: Health | None = None


class Condition(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str = Field(max_length=256)
    message: str | None = Field(default=None, max_length=8192)
    lastTransitionTime: AwareDatetime | None = None


class Operation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    phase: str | None = Field(default=None, max_length=256)
    message: str | None = Field(default=None, max_length=8192)
    revision: str | None = Field(default=None, max_length=256)
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    resources: list[Resource] = Field(default_factory=list, max_length=100)


class ArgoCdEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    app: str = Field(min_length=1, max_length=256)
    env: Literal["aws", "gcp", "local"]
    health: str = Field(min_length=1, max_length=256)
    images: list[str] = Field(default_factory=list, max_length=100)
    revision: str | None = Field(default=None, max_length=256)
    schema_version: Literal["deployment.result/v1"] | None = None
    event_type: Literal["deployed", "health_degraded", "sync_failed"] | None = None
    argocd_app: str | None = Field(default=None, max_length=256)
    cluster_id: str | None = Field(default=None, max_length=2048)
    namespace: str | None = Field(default=None, max_length=256)
    health_message: str | None = Field(default=None, max_length=8192)
    sync_status: str | None = Field(default=None, max_length=256)
    observed_at: AwareDatetime | None = None
    operation: Operation | None = None
    conditions: list[Condition] = Field(default_factory=list, max_length=100)
    resources: list[Resource] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_version(self):
        if bool(self.schema_version) != bool(self.event_type):
            raise ValueError("schema_version and event_type must be provided together")
        if any(len(i) > 2048 for i in self.images):
            raise ValueError("image too long")
        return self

    def image_tags(self):
        tags = []
        for image in self.images:
            last = image.split("@", 1)[0].rsplit("/", 1)[-1]
            if ":" in last:
                tags.append(last.rsplit(":", 1)[1])
        return tags

    def kind(self):
        if self.event_type:
            # Preserve explicit failure event semantics, but never label observed failures as deployed.
            if self.event_type == "deployed":
                if self.operation and self.operation.phase in {"Failed", "Error"}:
                    return "sync_failed"
                if self.health == "Degraded":
                    return "health_degraded"
            return self.event_type
        if self.operation and self.operation.phase in {"Failed", "Error"}:
            return "sync_failed"
        return {"Healthy": "deployed", "Degraded": "health_degraded"}.get(self.health)

    def identifiers(self):
        op = self.operation
        revision = op.revision if op else None
        # Health events may have no operation. An incomplete identity includes stable evidence,
        # preventing independent failures from being merged by app name alone.
        body = self.model_dump(mode="json", exclude_none=True)
        body.pop("observed_at", None)
        body["images"] = sorted(body.get("images", []))
        identity = [self.app, self.env, self.argocd_app, self.cluster_id, self.namespace,
                    revision, op.started_at.isoformat() if op and op.started_at else None]
        if not revision or not (op and op.started_at):
            identity += [self.revision, sorted(self.images), "identity_incomplete"]
        attempt = digest(identity)
        return "de_" + digest([attempt, self.kind(), body]), attempt


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()



def versioned_schema():
    schema = ArgoCdEvent.model_json_schema()
    schema["title"] = "deployment.result/v1"
    schema["required"] = ["schema_version", "event_type", "app", "env", "health"]
    schema["properties"]["schema_version"] = {"const": "deployment.result/v1"}
    schema["properties"]["event_type"] = {"enum": ["deployed", "health_degraded", "sync_failed"]}
    return schema
