from __future__ import annotations

import copy
import pickle
import shutil
import subprocess
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from review_ai.overlay import RenderedOverlay, overlay_diff, render_overlay
from review_ai.overlay.warnings import BLOCKING, RenderWarning, WarningCode
from review_ai.spec.deploy_spec import DeploySpec
from tests.conftest import load_sample_dict

FIXTURE_APPS = Path(__file__).parent / "fixtures" / "gitops" / "apps"
needs_kubectl = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl 없음")


def spec_of(name: str, **changes: Any) -> DeploySpec:
    raw = load_sample_dict(name)
    raw.update(changes)
    return DeploySpec.model_validate(raw)


def codes(rendered: RenderedOverlay) -> set[str]:
    return {w.code for w in rendered.warnings}


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


CI_TAGGED_IMAGE = "ghcr.io/crystal-sbhackathon2026/sample-app:b084c24e5a45f307981a9a005fa9d4e1e1079062"


def _normalize(docs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """팀 overlay 와 의도적으로 다른 한 곳(종료 유예 명시)을 지운다."""
    docs = copy.deepcopy(docs)
    docs["Rollout"]["spec"]["template"]["spec"].pop("terminationGracePeriodSeconds", None)
    return docs


@needs_kubectl
def test_sample_app_aws_reproduces_team_overlay(tmp_path: Path) -> None:
    ours = build(write_rendered(tmp_path, spec_of("01-pass-sample-app-aws.yaml")))
    team = build(FIXTURE_APPS / "sample-app" / "overlays" / "aws")
    assert _normalize(ours) == _normalize(team)


@needs_kubectl
def test_image_tag_comes_from_ci_not_spec(tmp_path: Path) -> None:
    """이미지 태그는 CI 가 base 에 쓴다. 명세의 tag·digest 로 덮으면 CI 태그 갱신이 무시된다."""
    rendered = render_overlay(spec_of("01-pass-sample-app-aws.yaml"))
    assert "images" not in yaml.safe_load(rendered.files["kustomization.yaml"])
    ours = build(write_rendered(tmp_path, spec_of("01-pass-sample-app-aws.yaml")))
    assert ours["Rollout"]["spec"]["template"]["spec"]["containers"][0]["image"] == CI_TAGGED_IMAGE


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
    # ReadWriteOnce 는 한 노드에만 붙는다 — canary 의 새 Pod 를 옛 Pod 와 같은 노드에 띄운다 (Multi-Attach 방지)
    assert pod["affinity"] == {"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
        {"labelSelector": {"matchLabels": {"app": "sample-app"}}, "topologyKey": "kubernetes.io/hostname"}]}}
    # base Pod label 과 맞아야 첫 배포가 자기 자신으로 통과한다
    assert docs["Rollout"]["spec"]["template"]["metadata"]["labels"]["app"] == "sample-app"


@pytest.mark.parametrize(("volume", "expected"), [
    ({"persistent": True}, True),
    ({"persistent": True, "access_mode": "ReadWriteMany"}, False),  # 여러 노드에 붙는다
    ({"persistent": False}, False),  # emptyDir 은 Pod 마다 따로
])
def test_same_node_affinity_only_for_read_write_once_pvc(volume: dict[str, Any], expected: bool) -> None:
    raw = load_sample_dict("02-pass-local-sqlite.yaml")
    raw["database"] = {"engine": "none"}
    raw["storage"] = {"volumes": [{"name": "uploads", "mount_path": "/u", "size": "1Gi", **volume}]}
    patch = yaml.safe_load(render_overlay(DeploySpec.model_validate(raw)).files["kustomization.yaml"])["patches"][0]
    paths = {op["path"] for op in yaml.safe_load(patch["patch"])}
    assert ("/spec/template/spec/affinity" in paths) is expected


def test_secrets_render_as_secret_key_refs_without_values() -> None:
    spec = spec_of("04-human-engine-change-with-data.yaml")
    rendered = render_overlay(spec)
    text = rendered.files["kustomization.yaml"]
    assert "secretKeyRef" in text and "orders-secrets" in text
    assert "orders/database-url" not in text
    assert any("orders-secrets" in w for w in rendered.warnings)  # RenderWarning 은 str 이라 문자열 검색도 된다
    assert codes(rendered) == {"SECRET_KEYS_REQUIRED", "DB_PROVISIONING_REQUIRED"}
    assert [w.code for w in rendered.blocking] == ["DB_PROVISIONING_REQUIRED"]


def test_buckets_and_allowed_cidrs_are_reported_not_dropped() -> None:
    rendered = render_overlay(spec_of("07-fix-public-bucket.yaml"))
    assert "BUCKET_PROVISIONING_REQUIRED" in codes(rendered)
    local = render_overlay(spec_of("05-fix-engine-unsupported-local.yaml"))
    assert "INGRESS_CIDRS_NOT_ENFORCED" in codes(local)


def test_unenforced_ingress_cidrs_block_commit() -> None:
    """막으라고 한 대역을 강제하지 못하면 공개로 열린다 — fail-closed (aws 는 ALB 가 강제하므로 경고 없음)."""
    local = render_overlay(spec_of("05-fix-engine-unsupported-local.yaml"))
    assert "INGRESS_CIDRS_NOT_ENFORCED" in {w.code for w in local.blocking}
    raw = load_sample_dict("05-fix-engine-unsupported-local.yaml")
    raw["target"] = {**raw["target"], "env": "aws"}
    aws = render_overlay(DeploySpec.model_validate(raw))
    assert "INGRESS_CIDRS_NOT_ENFORCED" not in codes(aws)


def test_local_demo_spec_has_no_blocking_warning() -> None:
    """로컬 데모 명세(02)는 allowed_cidrs 없이 커밋할 수 있다 — 대역을 넣으면 local 은 멈춘다 (05)."""
    rendered = render_overlay(spec_of("02-pass-local-sqlite.yaml"))
    assert rendered.blocking == ()
    assert "SNAT" in next(w for w in render_overlay(spec_of("05-fix-engine-unsupported-local.yaml")).warnings
                          if w.code == "INGRESS_CIDRS_NOT_ENFORCED")


def test_sample_app_has_no_blocking_warning() -> None:
    """데모 경로: 태그·digest 없이 쓴 sample-app 은 경고 없이 커밋할 수 있다 (이미지는 CI 몫)."""
    raw = load_sample_dict("01-pass-sample-app-aws.yaml")
    raw["image"] = {k: v for k, v in raw["image"].items() if k not in ("tag", "digest")}
    rendered = render_overlay(DeploySpec.model_validate(raw))
    assert rendered.warnings == ()


def test_aws_volume_uses_ebs_storage_class_and_warns_retained() -> None:
    """2026-10-10: EKS 는 oneaction-monitoring-gp3(EBS, Retain)로 ReadWriteOnce PVC 를 만든다. 배포는 막지 않는다."""
    raw = load_sample_dict("01-pass-sample-app-aws.yaml")
    raw["storage"] = {"volumes": [{"name": "data", "mount_path": "/data", "size": "1Gi"}]}
    rendered = render_overlay(DeploySpec.model_validate(raw))
    claim = yaml.safe_load(rendered.files["pvc-data.yaml"])["spec"]
    assert claim == {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}},
                     "storageClassName": "oneaction-monitoring-gp3"}
    assert rendered.blocking == ()
    assert [w.code for w in rendered.warnings] == ["VOLUME_RETAINED"]
    assert "data 1Gi" in rendered.warnings[0]


def test_aws_volume_read_write_many_still_blocks() -> None:
    raw = load_sample_dict("01-pass-sample-app-aws.yaml")
    raw["storage"] = {"volumes": [{"name": "data", "mount_path": "/data", "size": "1Gi", "access_mode": "ReadWriteMany"}]}
    assert [w.code for w in render_overlay(DeploySpec.model_validate(raw)).blocking] == ["VOLUME_UNSUPPORTED"]


def test_local_volume_uses_default_class_without_retained_warning() -> None:
    rendered = render_overlay(DeploySpec.model_validate(load_sample_dict("02-pass-local-sqlite.yaml")))
    claims = [yaml.safe_load(c)["spec"] for f, c in rendered.files.items() if f.startswith("pvc-")]
    assert claims and all("storageClassName" not in c for c in claims)
    assert not codes(rendered) & {"VOLUME_UNSUPPORTED", "VOLUME_RETAINED"}


def test_tls_without_host_blocks_tls_secret_does_not() -> None:
    raw = load_sample_dict("05-fix-engine-unsupported-local.yaml")
    raw["network"]["ingress"] = {"public": True, "tls": True}
    assert "TLS_HOST_MISSING" in {w.code for w in render_overlay(DeploySpec.model_validate(raw)).blocking}
    raw["network"]["ingress"] = {"public": True, "tls": True, "host": "a.example.com"}
    rendered = render_overlay(DeploySpec.model_validate(raw))
    assert "TLS_SECRET_REQUIRED" in codes(rendered)
    assert "TLS_SECRET_REQUIRED" not in {w.code for w in rendered.blocking}


def test_render_warning_is_a_str_with_code_and_survives_pickle() -> None:
    w = RenderWarning("DB_PROVISIONING_REQUIRED", "설명")
    assert (w, w.code, w.blocking) == ("설명", "DB_PROVISIONING_REQUIRED", True)
    copied = pickle.loads(pickle.dumps(w))
    assert (copied.code, copied.blocking, str(copied)) == (w.code, True, "설명")
    assert w.to_dict() == {"code": "DB_PROVISIONING_REQUIRED", "blocking": True, "message": "설명",
                           "doc": "warnings/DB_PROVISIONING_REQUIRED.md"}
    assert set(BLOCKING) == set(get_args(WarningCode))


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
