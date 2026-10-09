"""배포 계획 — 스키마 변경 종류로 마이그레이션 시점·전략을 고르고, 규칙(RUN-006·007)과 렌더러가 그대로 따르는지."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from review_ai.deploy_plan import choose_strategy, migration_hook, mixed_versions_unsafe
from review_ai.overlay import render_overlay
from review_ai.recommendations import build_recommendations
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import run_static_check
from tests.conftest import load_sample_dict
from tests.test_overlay import CI_TAGGED_IMAGE, needs_kubectl, write_rendered

MIGRATE = ["npm", "run", "migrate"]


def postgres_app(change: str | None = None, strategy: str | None = None, **extra: Any) -> dict[str, Any]:
    """sample-app 에 관리형 postgres 를 붙인 명세. change 를 주면 마이그레이션을 넣는다."""
    raw = load_sample_dict("01-pass-sample-app-aws.yaml")
    raw["database"] = {"engine": "postgres", "version": "16", "placement": "managed"}
    raw["secrets"] = [{"name": "DATABASE_URL", "source": "aws-secrets-manager", "key": "sample/database-url"}]
    if change is not None:
        raw["database"]["migration"] = {"command": MIGRATE, "change": change}
    if strategy is not None:
        raw["rollout"] = {"strategy": strategy}
    raw.update(extra)
    return raw


def with_baseline(raw: dict[str, Any], database: dict[str, Any], *, has_data: bool) -> dict[str, Any]:
    previous = {k: v for k, v in raw.items() if k != "baseline"}
    previous = {**previous, "database": database}
    return {**raw, "baseline": {"spec_ref": "review-prev", "spec": previous,
                                "facts": {"database_has_data": has_data}}}


def rule_ids(raw: dict[str, Any]) -> set[str]:
    return {f["rule_id"] for f in run_static_check(DeploySpec.model_validate(raw))}


@pytest.mark.parametrize(("change", "hook"), [("none", "PreSync"), ("expand", "PreSync"),
                                              ("breaking", "PreSync"), ("contract", "PostSync")])
def test_only_contract_runs_after_the_new_version(change: str, hook: str) -> None:
    assert migration_hook(change) == hook  # type: ignore[arg-type]


@pytest.mark.parametrize(("change", "strategy"), [(None, "canary"), ("none", "canary"), ("expand", "canary"),
                                                  ("contract", "canary"), ("breaking", "bluegreen")])
def test_strategy_follows_schema_change(change: str | None, strategy: str) -> None:
    spec = DeploySpec.model_validate(postgres_app(change))
    assert choose_strategy(spec, None) == strategy


def test_db_switch_from_previous_deploy_needs_bluegreen() -> None:
    sqlite = {"engine": "sqlite", "placement": "volume", "volume": "data"}
    spec = DeploySpec.model_validate(with_baseline(postgres_app(), sqlite, has_data=False))
    assert "두 버전이 다른 DB" in (mixed_versions_unsafe(spec, spec.baseline.spec) or "")
    assert choose_strategy(spec, spec.baseline.spec) == "bluegreen"


def test_first_db_or_same_db_keeps_canary() -> None:
    first = DeploySpec.model_validate(with_baseline(postgres_app(), {"engine": "none"}, has_data=False))
    same = DeploySpec.model_validate(
        with_baseline(postgres_app(), {"engine": "postgres", "version": "16", "placement": "managed"}, has_data=True))
    assert choose_strategy(first, first.baseline.spec) == "canary"
    assert choose_strategy(same, same.baseline.spec) == "canary"


def test_migration_is_only_for_job_reachable_databases() -> None:
    raw = load_sample_dict("02-pass-local-sqlite.yaml")
    raw["database"] = {**raw["database"], "migration": {"command": MIGRATE, "change": "expand"}}
    with pytest.raises(ValidationError, match="database.migration"):
        DeploySpec.model_validate(raw)


def test_migration_change_must_be_declared() -> None:
    raw = postgres_app()
    raw["database"]["migration"] = {"command": MIGRATE}
    with pytest.raises(ValidationError, match="change"):
        DeploySpec.model_validate(raw)


def test_breaking_change_under_canary_hits_both_rules() -> None:
    assert {"RUN-006", "RUN-007"} <= rule_ids(postgres_app("breaking"))


def test_breaking_change_under_bluegreen_still_needs_a_human() -> None:
    ids = rule_ids(postgres_app("breaking", "bluegreen"))
    assert "RUN-006" in ids and "RUN-007" not in ids


@pytest.mark.parametrize("change", ["none", "expand", "contract"])
def test_compatible_changes_raise_no_plan_finding(change: str) -> None:
    assert not {"RUN-006", "RUN-007"} & rule_ids(postgres_app(change))


def test_engine_change_with_data_is_left_to_db_001() -> None:
    mysql = {"engine": "mysql", "version": "8.0", "placement": "managed"}
    ids = rule_ids(with_baseline(postgres_app(), mysql, has_data=True))
    assert "DB-001" in ids and "RUN-007" not in ids


def test_engine_change_without_data_recommends_bluegreen() -> None:
    sqlite = {"engine": "sqlite", "placement": "volume", "volume": "data"}
    raw = with_baseline(postgres_app(), sqlite, has_data=False)
    spec = DeploySpec.model_validate(raw)
    findings = run_static_check(spec)
    [run_007] = [f for f in findings if f["rule_id"] == "RUN-007"]
    assert run_007["autofix"] == "allowed"
    [recommendation] = [r for r in build_recommendations(raw, findings) if run_007["finding_id"] in r["finding_ids"]]
    assert recommendation["ops"] == [{"op": "add", "path": "/rollout", "value": {"strategy": "bluegreen"}}]


def test_canary_renders_no_plan_files() -> None:
    rendered = render_overlay(DeploySpec.model_validate(postgres_app("expand")))
    assert "service-preview.yaml" not in rendered.files
    assert "blueGreen" not in rendered.files["kustomization.yaml"]


def _build_all(overlay_dir: Path) -> list[dict[str, Any]]:
    out = subprocess.run(["kubectl", "kustomize", str(overlay_dir)], check=True, capture_output=True, text=True).stdout
    return list(yaml.safe_load_all(out))


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


@needs_kubectl
def test_bluegreen_switches_strategy_and_adds_preview_service(tmp_path: Path) -> None:
    docs = _build_all(write_rendered(tmp_path, DeploySpec.model_validate(postgres_app("breaking", "bluegreen"))))
    strategy = _one(docs, "Rollout", "sample-app")["spec"]["strategy"]
    assert set(strategy) == {"blueGreen"}  # base 의 canary 설정이 남지 않는다
    assert strategy["blueGreen"]["activeService"] == "sample-app"
    assert strategy["blueGreen"]["previewService"] == "sample-app-preview"
    preview = _one(docs, "Service", "sample-app-preview")
    assert preview["metadata"]["namespace"] == "sample-app"
    assert preview["spec"]["ports"][0]["targetPort"] == 8080


@needs_kubectl
@pytest.mark.parametrize(("change", "hook"), [("expand", "PreSync"), ("contract", "PostSync")])
def test_migration_job_runs_the_deployed_image_as_a_hook(tmp_path: Path, change: str, hook: str) -> None:
    docs = _build_all(write_rendered(tmp_path, DeploySpec.model_validate(postgres_app(change))))
    job = _one(docs, "Job", "sample-app-migrate")
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == hook
    assert job["metadata"]["namespace"] == "sample-app"
    [container] = job["spec"]["template"]["spec"]["containers"]
    assert container["image"] == CI_TAGGED_IMAGE  # 앱과 같은 버전 — 명세의 태그가 아니라 CI 가 쓴 태그
    assert container["command"] == MIGRATE
    secret = next(e for e in container["env"] if e["name"] == "DATABASE_URL")
    assert secret["valueFrom"]["secretKeyRef"] == {"name": "sample-app-secrets", "key": "DATABASE_URL"}
    assert job["spec"]["backoffLimit"] == 0


def test_spec_without_rollout_or_migration_renders_as_before() -> None:
    raw = load_sample_dict("01-pass-sample-app-aws.yaml")
    rendered = render_overlay(DeploySpec.model_validate(raw))
    assert set(rendered.files) == {"ingress.yaml", "kustomization.yaml"}
    assert "replacements" not in rendered.files["kustomization.yaml"]
