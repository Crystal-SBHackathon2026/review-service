"""명세 없음·빈 명세·형식 오류 처리 결정 — 생성은 확인된 값만, 추정값·가린 값은 커밋하지 않는다."""

from __future__ import annotations

import copy

import pytest
import yaml

from review_ai.intake import commit_message, prepare_intake
from review_ai.preparation import GenerationContext
from review_ai.secrets_pattern import MASK
from review_ai.spec.deploy_spec import AppSpec, Baseline, DeploySpec


def bare_context(spec: dict) -> GenerationContext:
    return GenerationContext(repository=spec["metadata"]["repository"], target=spec["target"])


def same_settings(a: AppSpec, b: AppSpec) -> bool:
    """metadata.commit(자기 커밋)은 생성 파일이 모른다 — 나머지 설정이 같은지."""
    skip = {"metadata": {"commit"}, "baseline": True}
    return a.model_dump(exclude=skip) == b.model_dump(exclude=skip)


@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_baseline_regenerates_committable_spec(kind: str, sample_app: dict) -> None:
    outcome = prepare_intake(kind, context=bare_context(sample_app), baseline=Baseline(spec_ref="m1", spec=sample_app))

    assert (outcome.action, outcome.reason) == ("generated", "GENERATED")
    assert outcome.content.startswith("# review-service 가 생성한 명세")
    regenerated = DeploySpec.model_validate(yaml.safe_load(outcome.content))
    assert same_settings(regenerated, AppSpec.model_validate(sample_app))  # 이전 승인 명세를 그대로 보존
    assert {d["source"] for d in outcome.details} == {"baseline"}


def test_new_app_without_verified_values_is_not_committed(sample_app: dict) -> None:
    outcome = prepare_intake("missing", context=bare_context(sample_app))

    assert (outcome.action, outcome.reason, outcome.content) == ("rejected", "UNVERIFIED", None)
    assert "RUNTIME_UNVERIFIED" in outcome.message
    assert {d["code"] for d in outcome.details} >= {"RUNTIME_UNVERIFIED", "DATABASE_UNVERIFIED"}


def test_verified_context_generates_without_baseline(sample_app: dict) -> None:
    app = AppSpec.model_validate(sample_app)
    context = GenerationContext(repository=app.metadata.repository, name=app.metadata.name, target=app.target,
                                **{f: getattr(app, f) for f in ("image", "runtime", "database", "requirements",
                                                                "secrets", "storage", "network")})
    outcome = prepare_intake("empty", context=context)

    assert outcome.action == "generated" and "확인된 앱 설정" in outcome.message
    assert same_settings(AppSpec.model_validate(yaml.safe_load(outcome.content)), app)


@pytest.mark.parametrize("kind", ["yaml_error", "schema_error"])
def test_broken_spec_is_rejected_until_repair_exists(kind: str, sample_app: dict) -> None:
    outcome = prepare_intake(kind, context=bare_context(sample_app))

    assert (outcome.action, outcome.reason, outcome.content) == ("rejected", "REPAIR_UNAVAILABLE", None)


def test_masked_baseline_is_not_committed(sample_app: dict) -> None:
    masked = copy.deepcopy(sample_app)
    masked["runtime"]["env"] = {"API_TOKEN": MASK}
    outcome = prepare_intake("missing", context=bare_context(masked), baseline=Baseline(spec_ref="m1", spec=masked))

    assert (outcome.action, outcome.reason, outcome.content) == ("rejected", "MASKED_VALUE", None)


def test_commit_message_lists_sources(sample_app: dict) -> None:
    outcome = prepare_intake("missing", context=bare_context(sample_app), baseline=Baseline(spec_ref="m1", spec=sample_app))
    message = commit_message(outcome)

    title, blank, *body = message.splitlines()
    assert title == "chore: deploy.yaml 없음 — 이전 승인 명세(baseline)로 deploy.yaml 생성" and blank == ""
    assert "- /runtime: baseline — 이전 승인 명세의 설정을 보존한다" in body
