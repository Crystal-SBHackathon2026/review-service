"""Operator-authorized diagnosis and resolution APIs (REVIEW_API_TOKEN grants operator scope)."""
from datetime import UTC, datetime
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from review_ai.deployment_analysis import config_value
from review_ai.failure_evidence import safe_data


class ResolutionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success_event_id: str = Field(pattern=r"^de_[0-9a-f]{64}$")
    cause: str = Field(min_length=1, max_length=2000)
    actions: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(min_length=1, max_length=10)
    config_paths: list[Annotated[str, Field(max_length=256)]] = Field(default_factory=list, max_length=10)


async def resolve_case(repo, case_id, body):
    case = await repo.get_failure_case(case_id)
    failure = await repo.get_deployment(case_id)
    success = await repo.get_deployment(body.success_event_id)
    if case is None or failure is None:
        raise HTTPException(404, "실패 사례가 없습니다")
    if case.get("resolution"):
        raise HTTPException(409, "이미 해결 확인된 사례입니다")
    if not success or success["kind"] != "deployed" or not success["review_id"] or not success["spec_snapshot"]:
        raise HTTPException(422, "명세와 연결된 성공 배포 증거가 필요합니다")
    p = success["payload"]
    if p.get("health") != "Healthy" or p.get("sync_status") != "Synced" or (p.get("operation") or {}).get("phase") != "Succeeded":
        raise HTTPException(422, "정상 배포 완료 증거가 필요합니다")
    if (case["app"], case["repository"], case["target_env"]) != (success["app"], success["repository"], success["target_env"]):
        raise HTTPException(422, "같은 앱·레포·환경의 배포여야 합니다")
    if success["review_id"] == failure["review_id"] or success["occurred_at"] <= failure["occurred_at"]:
        raise HTTPException(422, "실패 후의 새 수정 배포가 필요합니다")
    for key in ("cluster_id", "namespace"):
        if failure["payload"].get(key) != success["payload"].get(key):
            raise HTTPException(422, "동일한 배포 대상의 결과여야 합니다")
    if await repo.has_failed_deployment(success["review_id"]):
        raise HTTPException(422, "실패가 관측된 배포는 해결 근거로 사용할 수 없습니다")
    conditions = []
    try:
        for path in dict.fromkeys(body.config_paths):
            failed = config_value(case["failed_spec"], path)
            fixed = config_value(success["spec_snapshot"], path)
            if failed == fixed:
                raise ValueError("unchanged condition")
            conditions.append(dict(path=path, failed_value=failed, resolved_value=fixed))
    except (ValueError, KeyError, IndexError, TypeError):
        raise HTTPException(422, "관련 경로는 두 명세에서 확인 가능한 변경된 비밀 아닌 설정값이어야 합니다")
    resolution = safe_data(dict(success_event_id=body.success_event_id, cause=body.cause, actions=body.actions,
                                conditions=conditions, confirmed_by="review-api-operator", confirmed_at=datetime.now(UTC).isoformat()))
    if not await repo.confirm_resolution(case_id, resolution):
        raise HTTPException(409, "이미 해결 확인된 사례입니다")
    return dict(case_id=case_id, resolution=resolution)
