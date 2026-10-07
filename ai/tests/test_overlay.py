from __future__ import annotations

import copy
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from review_ai.overlay import overlay_diff, render_overlay
from review_ai.spec.deploy_spec import DeploySpec
from tests.conftest import load_sample_dict

FIXTURE_APPS = Path(__file__).parent / "fixtures" / "gitops" / "apps"
needs_kubectl = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl 없음")


def spec_of(name: str, **changes: Any) -> DeploySpec:
    raw = load_sample_dict(name)
    raw.update(changes)
    return DeploySpec.model_validate(raw)


def build(overlay_dir: Path) -> dict[str, dict[str, Any]]:
    out = subprocess.run(["kubectl", "kustomize", str(overlay_dir)], check=True, capture_output=True, text=True).stdout
    return {doc["kind"]: doc for doc in yaml.safe_load_all(out)}


def write_rendered(tmp_path: Path, spec: DeploySpec) -> Path:
    apps = tmp_path / "apps"
    shutil.copytree(FIXTURE_APPS / "sample-app" / "base", apps / spec.metadata.name / "base")
    rendered = render_overlay(spec)
    target = tmp_path / rendered.directory
    target.mkdir(parents=True)
    for filename, content in rendered.files.items():
        (target / filename).write_text(content, encoding="utf-8")
    return target


def _normalize(docs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """팀 overlay 와 의도적으로 다른 두 곳(이미지 digest 고정, 종료 유예 명시)을 지운다."""
    docs = copy.deepcopy(docs)
    pod = docs["Rollout"]["spec"]["template"]["spec"]
    pod.pop("terminationGracePeriodSeconds", None)
    pod["containers"][0].pop("image")
    return docs


@needs_kubectl
def test_sample_app_aws_reproduces_team_overlay(tmp_path: Path) -> None:
    ours = build(write_rendered(tmp_path, spec_of("01-pass-sample-app-aws.yaml")))
    team = build(FIXTURE_APPS / "sample-app" / "overlays" / "aws")
    assert _normalize(ours) == _normalize(team)
    image = ours["Rollout"]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.endswith("@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")


TEAM_ENVS = {
    "gcp": ({"env": "gcp", "region": "asia-northeast1", "namespace": "sample-app"}, None),
    "local": ({"env": "local", "region": "busan-local", "namespace": "sample-app"}, {"public": True, "tls": False}),
}


@needs_kubectl
@pytest.mark.parametrize("env", sorted(TEAM_ENVS))
def test_sample_app_other_envs_reproduce_team_overlay(tmp_path: Path, env: str) -> None:
    target, ingress = TEAM_ENVS[env]
    raw = load_sample_dict("01-pass-sample-app-aws.yaml")
    raw.update(target=target, network={"ingress": ingress})
    raw["runtime"]["env"] = {"DEPLOY_ENV": env, "DEPLOY_REGION": target["region"]}
    ours = build(write_rendered(tmp_path, DeploySpec.model_validate(raw)))
    team = build(FIXTURE_APPS / "sample-app" / "overlays" / env)
    assert _normalize(ours) == _normalize(team)


@needs_kubectl
def test_sqlite_volume_renders_pvc_and_mount(tmp_path: Path) -> None:
    raw = load_sample_dict("02-pass-local-sqlite.yaml")
    raw["metadata"]["name"] = "sample-app"  # fixture base 를 재사용
    docs = build(write_rendered(tmp_path, DeploySpec.model_validate(raw)))
    pod = docs["Rollout"]["spec"]["template"]["spec"]
    assert pod["volumes"] == [{"name": "data", "persistentVolumeClaim": {"claimName": "sample-app-data"}}]
    assert pod["containers"][0]["volumeMounts"] == [{"name": "data", "mountPath": "/data"}]
    assert docs["PersistentVolumeClaim"]["spec"]["resources"]["requests"]["storage"] == "1Gi"
    assert docs["Service"]["spec"]["ports"][0]["targetPort"] == 3000
    assert docs["Ingress"]["spec"]["ingressClassName"] == "traefik"


def test_secrets_render_as_secret_key_refs_without_values() -> None:
    spec = spec_of("04-human-engine-change-with-data.yaml")
    rendered = render_overlay(spec)
    text = rendered.files["kustomization.yaml"]
    assert "secretKeyRef" in text and "orders-secrets" in text
    assert "orders/database-url" not in text
    assert any("orders-secrets" in w for w in rendered.warnings)
    assert any("database" in w for w in rendered.warnings)


def test_buckets_and_allowed_cidrs_are_reported_not_dropped() -> None:
    rendered = render_overlay(spec_of("07-fix-public-bucket.yaml"))
    assert any("buckets" in w for w in rendered.warnings)
    local = render_overlay(spec_of("05-fix-engine-unsupported-local.yaml"))
    assert any("allowed_cidrs" in w for w in local.warnings)


def test_aws_tls_and_internal_annotations() -> None:
    spec = spec_of("06-human-plaintext-secret.yaml")
    ingress = yaml.safe_load(render_overlay(spec).files["ingress.yaml"])
    ann = ingress["metadata"]["annotations"]
    assert ann["alb.ingress.kubernetes.io/listen-ports"] == '[{"HTTP": 80}, {"HTTPS": 443}]'
    assert ann["alb.ingress.kubernetes.io/ssl-redirect"] == "443"
    assert ingress["spec"]["rules"][0]["host"] == "app.example.com"


def test_render_is_deterministic() -> None:
    spec = spec_of("03-fix-sqlite-replicas-gcp.yaml")
    assert render_overlay(spec) == render_overlay(spec)


def test_overlay_diff_shows_only_changed_files() -> None:
    before = spec_of("03-fix-sqlite-replicas-gcp.yaml")
    raw = load_sample_dict("03-fix-sqlite-replicas-gcp.yaml")
    raw["runtime"]["replicas"] = 1
    diffs = overlay_diff(before, DeploySpec.model_validate(raw))
    assert [d["path"] for d in diffs] == ["apps/todo/overlays/gcp/kustomization.yaml"]
    assert "-      value: 2" in diffs[0]["diff"] and "+      value: 1" in diffs[0]["diff"]


def test_render_refuses_masked_spec() -> None:
    from review_ai.masking import mask_spec

    raw = mask_spec(load_sample_dict("06-human-plaintext-secret.yaml"))
    with pytest.raises(ValueError, match="MASKED"):
        render_overlay(DeploySpec.model_validate(raw))
