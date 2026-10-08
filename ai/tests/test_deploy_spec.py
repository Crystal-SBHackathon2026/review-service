"""앱 레포에 커밋하는 deploy.yaml — 자기 커밋 SHA·이미지 태그를 모른다."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from review_ai.messages import build_review_requested
from review_ai.spec.deploy_spec import DeploySpec


def app_repo_spec(sample: dict[str, Any]) -> dict[str, Any]:
    """01 샘플에서 앱 레포 파일이 알 수 없는 값(metadata.commit·image.tag·image.digest)을 뺀 명세."""
    sample["metadata"].pop("commit")
    sample["image"].pop("tag")
    sample["image"].pop("digest")
    return sample


def test_spec_without_commit_is_valid(sample_app: dict[str, Any]) -> None:
    spec = DeploySpec.model_validate(app_repo_spec(sample_app))

    assert spec.metadata.commit is None


def test_spec_without_commit_builds_review_requested(sample_app: dict[str, Any]) -> None:
    ref = {"repository": "Crystal-SBHackathon2026/sample-app", "commit": "14411fd", "path": "deploy.yaml"}

    message = build_review_requested(app_repo_spec(sample_app), review_id="r", spec_ref=ref,
                                     requested_by="test", requested_at=datetime.now(UTC))

    assert message.spec_ref.commit == "14411fd"


def test_commit_still_must_be_a_sha_when_given(sample_app: dict[str, Any]) -> None:
    sample_app["metadata"]["commit"] = "main"

    with pytest.raises(ValidationError):
        DeploySpec.model_validate(sample_app)
