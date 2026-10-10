"""데이터 이전 — SQLite(볼륨) → Postgres 를 마이그레이션 다음에 한 번만. 명세 검사·규칙(DB-009)·렌더링."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from review_ai.overlay import render_overlay
from review_ai.overlay.data_import import DUMP_IMAGE, LOAD_IMAGE, import_source
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import run_static_check
from tests.conftest import load_sample_dict
from tests.test_deploy_plan import _build_all, _one
from tests.test_overlay import needs_kubectl, write_rendered

SQLITE = {"engine": "sqlite", "placement": "volume", "volume": "data", "env_var": "DATABASE_PATH"}


def converted(data_import: dict[str, Any] | None = None, *, has_data: bool | None = True,
              change: str = "expand", keep_volume: bool = True) -> dict[str, Any]:
    """local sqlite 앱(샘플 02)을 in-cluster postgres 로 바꾸는 명세. baseline 은 원래 sqlite."""
    previous = load_sample_dict("02-pass-local-sqlite.yaml")
    raw = {**previous, "database": {"engine": "postgres", "placement": "in-cluster", "version": "16",
                                    "migration": {"command": ["node", "src/migrate.js"], "change": change}},
           "secrets": [{"name": "DATABASE_URL", "source": "k8s-secret", "key": "database-url"}],
           "rollout": {"strategy": "bluegreen"}}
    if data_import is not None:
        raw["database"]["data_import"] = data_import
    if not keep_volume:
        raw["storage"] = {}
    raw["baseline"] = {"spec_ref": "prev", "spec": previous, "facts": {"database_has_data": has_data}}
    return raw


IMPORT = {"from_volume": "data", "file": "todo.db"}


def rule_ids(raw: dict[str, Any]) -> set[str]:
    return {f["rule_id"] for f in run_static_check(DeploySpec.model_validate(raw))}


def test_sample_02_is_local_sqlite_on_volume_data() -> None:
    raw = load_sample_dict("02-pass-local-sqlite.yaml")
    assert raw["database"]["engine"] == "sqlite" and raw["database"]["volume"] == "data"


def test_engine_change_with_data_and_no_import_plan_hits_db_009() -> None:
    ids = rule_ids(converted())
    assert {"DB-001", "DB-009"} <= ids


def test_import_plan_clears_db_009_but_db_001_still_needs_a_human() -> None:
    ids = rule_ids(converted(IMPORT))
    assert "DB-009" not in ids and "DB-001" in ids  # 엔진 변경은 되돌릴 수 없다 — 사람이 승인한다


@pytest.mark.parametrize("has_data", [False])
def test_no_data_needs_no_import_plan(has_data: bool) -> None:
    assert "DB-009" not in rule_ids(converted(has_data=has_data))


@pytest.mark.parametrize(("raw", "message"), [
    (lambda: converted(IMPORT, change="contract"), "change ≠ contract"),
    (lambda: converted(IMPORT, keep_volume=False), "persistent 볼륨"),
    (lambda: converted({"from_volume": "uploads", "file": "todo.db"}), "persistent 볼륨"),
    (lambda: converted({"from_volume": "data", "file": "../etc/passwd"}), "file"),
    (lambda: {**converted(IMPORT), "database": {**converted(IMPORT)["database"], "migration": None}}, "migration"),
])
def test_import_plan_is_validated(raw: Any, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        DeploySpec.model_validate(raw())


def test_without_import_no_job_is_rendered() -> None:
    assert "job-data-import.yaml" not in render_overlay(DeploySpec.model_validate(converted())).files


@needs_kubectl
def test_import_job_runs_after_migration_before_the_new_version(tmp_path: Path) -> None:
    raw = converted(IMPORT)  # 빌드 픽스처의 base 는 sample-app — 앱 이름을 맞춘다
    for doc in (raw, raw["baseline"]["spec"]):
        doc["metadata"] = {**doc["metadata"], "name": "sample-app"}
    spec = DeploySpec.model_validate(raw)
    docs = _build_all(write_rendered(tmp_path, spec))
    job = _one(docs, "Job", "sample-app-data-import")
    annotations = job["metadata"]["annotations"]
    assert (annotations["argocd.argoproj.io/hook"], annotations["argocd.argoproj.io/sync-wave"]) == ("PreSync", "1")
    migrate = _one(docs, "Job", "sample-app-migrate")["metadata"]["annotations"]
    assert migrate["argocd.argoproj.io/hook"] == "PreSync" and migrate.get("argocd.argoproj.io/sync-wave", "0") == "0"

    pod = job["spec"]["template"]["spec"]
    assert {"name": "source", "persistentVolumeClaim": {"claimName": "sample-app-data", "readOnly": True}} in pod["volumes"]
    [dump], [load] = pod["initContainers"], pod["containers"]
    assert (dump["image"], load["image"]) == (DUMP_IMAGE, LOAD_IMAGE)
    assert dump["command"][-1] == "todo.db"  # 파일 이름은 인자로 — 스크립트에 끼워 넣지 않는다
    assert {"name": "source", "mountPath": "/source", "readOnly": True} in dump["volumeMounts"]
    assert load["command"][-1] == import_source(spec) == "sqlite:sample-app/data/todo.db"
    assert load["env"] == [{"name": "DATABASE_URL",
                            "valueFrom": {"secretKeyRef": {"name": "sample-app-secrets", "key": "DATABASE_URL"}}}]
    assert load["securityContext"]["runAsNonRoot"] and dump["securityContext"]["readOnlyRootFilesystem"]
    assert job["spec"]["backoffLimit"] == 0
    # PreSync 동안 옛 앱 Pod 가 PVC 를 붙이고 있다 — 같은 노드를 선호한다 (앱 Pod 가 없을 때 Pending 이 되지 않게 preferred)
    assert pod["affinity"] == {"podAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
        {"weight": 100, "podAffinityTerm": {"labelSelector": {"matchLabels": {"app": "sample-app"}},
                                            "topologyKey": "kubernetes.io/hostname"}}]}}
    # PVC 는 overlay 에 남아 있다 — 옮기기 전에 지워지지 않는다
    assert _one(docs, "PersistentVolumeClaim", "sample-app-data")
