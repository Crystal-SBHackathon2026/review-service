"""PR에 기록하는 환경 선택 계약. 환경별 설정은 각각의 AppSpec 파일이 정본이다."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from review_ai.spec.deploy_spec import Provider

SPEC_PATH = r"^(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.ya?ml$"


class SelectedTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    env: Provider
    region: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")
    cluster: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    path: str = Field(pattern=SPEC_PATH, max_length=200)


class DeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    api_version: Literal["oneaction/v1"] = Field(alias="apiVersion")
    kind: Literal["DeploymentRequest"]
    targets: tuple[SelectedTarget, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_targets(self):
        if len({t.env for t in self.targets}) != len(self.targets):
            raise ValueError("환경은 한 번만 선택할 수 있다")
        if len({t.path for t in self.targets}) != len(self.targets):
            raise ValueError("환경마다 별도의 명세 파일이 필요하다")
        return self


def registered_targets(raw: str | None) -> frozenset[tuple[str, str, str]]:
    """서버 설정의 env/region/Argo destination name allowlist. 비면 다중 환경은 허용하지 않는다."""
    if not raw:
        return frozenset()
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ValueError("DEPLOYMENT_TARGETS_JSON must be an array")
    allowed = set()
    for row in rows:
        target = SelectedTarget.model_validate({**row, "path": "deploy.yaml"})
        allowed.add((target.env, target.region, target.cluster))
    return frozenset(allowed)
