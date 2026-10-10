from __future__ import annotations

import copy
from typing import Any

import pytest

from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import make_static_check, run_static_check
from tests.conftest import load_cases, load_sample_dict


def rule_ids(spec: dict[str, Any]) -> list[str]:
    return sorted(f["rule_id"] for f in run_static_check(DeploySpec.model_validate(spec)))


def findings_of(spec: dict[str, Any], rule_id: str) -> list[dict[str, Any]]:
    return [f for f in run_static_check(DeploySpec.model_validate(spec)) if f["rule_id"] == rule_id]


# ── 정답지: cases.yaml ────────────────────────────────────────────


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["file"])
def test_sample_findings_match_cases(case: dict[str, Any]) -> None:
    assert rule_ids(load_sample_dict(case["file"])) == sorted(case["findings"])


@pytest.mark.parametrize(
    "case", [c for c in load_cases() if "evidence_must_not_contain" in c], ids=lambda c: c["file"]
)
def test_evidence_never_contains_secret_values(case: dict[str, Any]) -> None:
    spec = DeploySpec.model_validate(load_sample_dict(case["file"]))
    blob = repr(run_static_check(spec))
    for secret in case["evidence_must_not_contain"]:
        assert secret not in blob


def test_findings_are_deterministic_and_sorted() -> None:
    spec = DeploySpec.model_validate(load_sample_dict("10-human-mixed-aws.yaml"))
    first, second = run_static_check(spec), run_static_check(spec)
    assert first == second
    assert [f["rule_id"] for f in first] == sorted(f["rule_id"] for f in first)


def test_finding_id_is_stable_per_rule_and_location() -> None:
    spec = load_sample_dict("03-fix-sqlite-replicas-gcp.yaml")
    a = findings_of(spec, "DB-003")[0]
    spec["metadata"]["commit"] = "aaaaaaa"
    b = findings_of(spec, "DB-003")[0]
    assert a["finding_id"] == b["finding_id"]
    assert a["finding_id"].startswith("DB-003:")


def test_severity_and_category_come_from_catalog(sample_app: dict[str, Any]) -> None:
    f = findings_of(sample_app, "NET-001")[0]
    assert (f["severity"], f["category"], f["autofix"], f["irreversible"]) == ("low", "network", "forbidden", False)


# ── database ─────────────────────────────────────────────────────


def _with_baseline(spec: dict[str, Any], prev_db: dict[str, Any], has_data: bool | None) -> dict[str, Any]:
    prev = copy.deepcopy(spec)
    prev["database"] = prev_db
    facts = {} if has_data is None else {"database_has_data": has_data}
    return {**spec, "baseline": {"spec_ref": "prev", "spec": prev, "facts": facts}}


PG = {"engine": "postgres", "version": "16", "placement": "managed"}
MYSQL = {"engine": "mysql", "version": "8.0", "placement": "managed"}
DB_SECRET = [{"name": "DATABASE_URL", "source": "aws-secrets-manager", "key": "app/db"}]


@pytest.mark.parametrize(
    ("has_data", "expected"),
    [(True, ["DB-001"]), (None, ["DB-001"]), (False, [])],
    ids=["data", "unknown-means-data", "no-data"],
)
def test_db001_engine_change_depends_on_data(sample_app: dict[str, Any], has_data: bool | None, expected: list[str]) -> None:
    spec = {**sample_app, "database": MYSQL, "secrets": DB_SECRET}
    spec = _with_baseline(spec, PG, has_data)
    assert [r for r in rule_ids(spec) if r.startswith("DB")] == expected


def test_db001_is_irreversible_and_forbidden(sample_app: dict[str, Any]) -> None:
    spec = _with_baseline({**sample_app, "database": MYSQL, "secrets": DB_SECRET}, PG, True)
    f = findings_of(spec, "DB-001")[0]
    assert (f["autofix"], f["irreversible"], f["location"]["spec_path"]) == ("forbidden", True, "/database/engine")
    assert f["evidence"] == "postgres → mysql"


def test_db001_ignores_first_database(sample_app: dict[str, Any]) -> None:
    spec = _with_baseline({**sample_app, "database": PG, "secrets": DB_SECRET}, {"engine": "none"}, True)
    assert "DB-001" not in rule_ids(spec)


@pytest.mark.parametrize(
    ("db", "env", "hit"),
    [
        ({"engine": "mysql", "placement": "in-cluster"}, "local", True),
        ({"engine": "postgres", "placement": "in-cluster", "version": "16"}, "local", False),
        ({"engine": "postgres", "placement": "managed"}, "local", True),
        ({"engine": "postgres", "placement": "managed", "version": "9"}, "aws", True),
        ({"engine": "mysql", "placement": "managed", "version": "8.0"}, "aws", False),
        ({"engine": "sqlite", "placement": "managed"}, "aws", True),
    ],
)
def test_db002_target_capabilities(sample_app: dict[str, Any], db: dict[str, Any], env: str, hit: bool) -> None:
    spec = {**sample_app, "database": db, "secrets": DB_SECRET, "target": {"env": env, "region": "r"}}
    assert ("DB-002" in rule_ids(spec)) is hit


def test_db002_autofix_allowed_without_data_forbidden_with_data(sample_app: dict[str, Any]) -> None:
    db = {"engine": "mysql", "placement": "in-cluster"}
    spec = {**sample_app, "database": db, "secrets": DB_SECRET, "target": {"env": "local", "region": "r"}}
    assert findings_of(spec, "DB-002")[0]["autofix"] == "allowed"
    with_data = _with_baseline(spec, db, True)
    assert findings_of(with_data, "DB-002")[0]["autofix"] == "forbidden"


def test_db003_sqlite_single_replica_is_fine() -> None:
    spec = load_sample_dict("03-fix-sqlite-replicas-gcp.yaml")
    spec["runtime"]["replicas"] = 1
    assert rule_ids(spec) == []


@pytest.mark.parametrize(
    "volumes",
    [[], [{"name": "data", "mount_path": "/data", "size": "1Gi", "persistent": False}]],
    ids=["missing", "ephemeral"],
)
def test_db005_sqlite_needs_persistent_volume(volumes: list[dict[str, Any]]) -> None:
    spec = load_sample_dict("02-pass-local-sqlite.yaml")
    spec["storage"] = {"volumes": volumes}
    assert "DB-005" in rule_ids(spec)


@pytest.mark.parametrize(
    ("persistence", "db", "volumes", "hit"),
    [
        (True, {}, [], True),
        (True, {}, [{"name": "tmp", "mount_path": "/tmp/x", "size": "1Gi", "persistent": False}], True),
        (True, {}, [{"name": "data", "mount_path": "/data", "size": "1Gi"}], False),
        (True, {"engine": "postgres", "version": "16", "placement": "managed"}, [], False),
        (True, {}, "bucket", False),
        (False, {}, [], False),
    ],
    ids=["nothing", "ephemeral-only", "persistent-volume", "database", "bucket", "not-required"],
)
def test_db004_persistence_needs_a_store(
    sample_app: dict[str, Any], persistence: bool, db: dict[str, Any], volumes: Any, hit: bool
) -> None:
    storage = {"buckets": [{"name": "app-uploads"}]} if volumes == "bucket" else {"volumes": volumes}
    spec = {**sample_app, "requirements": {"persistence": persistence}, "database": db, "secrets": DB_SECRET,
            "storage": storage, "target": {"env": "local", "region": "r"}}
    found = findings_of(spec, "DB-004")
    assert bool(found) is hit
    if hit:
        assert (found[0]["autofix"], found[0]["location"]["spec_path"]) == ("forbidden", "/requirements/persistence")


@pytest.mark.parametrize(
    ("env", "db", "hit"),
    [
        ("aws", {**PG, "publicly_accessible": True}, True),
        ("aws", PG, False),
        ("gcp", {**PG, "placement": "in-cluster", "publicly_accessible": True}, False),  # 관리형만 공인 주소를 받는다
        ("local", {**PG, "publicly_accessible": True}, False),  # aws·gcp 규칙
    ],
)
def test_db006_public_managed_db(sample_app: dict[str, Any], env: str, db: dict[str, Any], hit: bool) -> None:
    spec = {**sample_app, "database": db, "secrets": DB_SECRET, "target": {"env": env, "region": "r"}}
    found = findings_of(spec, "DB-006")
    assert bool(found) is hit
    if hit:
        assert (found[0]["autofix"], found[0]["location"]["spec_path"]) == ("allowed", "/database/publicly_accessible")


# ── secret ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "value", "hit"),
    [
        ("DB_PASSWORD", "x", True),
        ("STRIPE_API_KEY", "x", True),
        ("CLIENT_SECRET", "x", True),
        ("REDIS_URL", "redis://user:pa55@redis:6379", True),
        ("REDIS_URL", "redis://redis:6379", False),
        ("LOG_LEVEL", "info", False),
    ],
)
def test_sec001_plaintext_secret(sample_app: dict[str, Any], name: str, value: str, hit: bool) -> None:
    sample_app["runtime"]["env"][name] = value
    found = findings_of(sample_app, "SEC-001")
    assert bool(found) is hit
    if hit:
        assert value not in found[0]["evidence"]
        assert found[0]["location"]["spec_path"] == f"/runtime/env/{name}"


def test_sec005_db_without_connection_secret(sample_app: dict[str, Any]) -> None:
    spec = {**sample_app, "database": PG}
    assert "SEC-005" in rule_ids(spec)
    assert "SEC-005" not in rule_ids({**spec, "secrets": DB_SECRET})


def test_sec004_env_shadowed_by_secret_hides_value(sample_app: dict[str, Any]) -> None:
    sample_app["runtime"]["env"]["UPSTREAM_URL"] = "https://old.example.com"
    sample_app["secrets"] = [{"name": "UPSTREAM_URL", "source": "k8s-secret", "key": "upstream"}]
    [f] = findings_of(sample_app, "SEC-004")
    assert (f["autofix"], f["location"]["spec_path"]) == ("allowed", "/runtime/env/UPSTREAM_URL")
    assert "old.example.com" not in f["evidence"]
    sample_app["runtime"]["env"].pop("UPSTREAM_URL")
    assert "SEC-004" not in rule_ids(sample_app)


# ── network · storage · runtime ─────────────────────────────────


def test_net001_only_for_public_without_tls(sample_app: dict[str, Any]) -> None:
    assert "NET-001" in rule_ids(sample_app)
    sample_app["network"]["ingress"] = {"public": True, "tls": True, "host": "a.example.com"}
    assert "NET-001" not in rule_ids(sample_app)
    sample_app["network"]["ingress"] = {"public": False}
    assert "NET-001" not in rule_ids(sample_app)


def test_sto003_skipped_on_local(sample_app: dict[str, Any]) -> None:
    sample_app["storage"] = {"buckets": [{"name": "uploads", "public": True}]}
    assert "STO-003" in rule_ids(sample_app)
    sample_app["target"] = {"env": "local", "region": "busan-local"}
    assert "STO-003" not in rule_ids(sample_app)


@pytest.mark.parametrize(("prev", "cur", "hit"), [("10Gi", "5Gi", True), ("1Gi", "1024Mi", False), ("1Gi", "2Gi", False)])
def test_sto005_volume_shrink(sample_app: dict[str, Any], prev: str, cur: str, hit: bool) -> None:
    vol = {"name": "uploads", "mount_path": "/u", "persistent": True}
    spec = {**sample_app, "storage": {"volumes": [{**vol, "size": cur}]}}
    prev_spec = {**sample_app, "storage": {"volumes": [{**vol, "size": prev}]}}
    spec["baseline"] = {"spec_ref": "prev", "spec": prev_spec}
    assert ("STO-005" in rule_ids(spec)) is hit


@pytest.mark.parametrize(
    ("env", "volume", "hit"),
    [
        ("aws", {"persistent": True}, True),  # EBS CSI 없음 — 지원 접근 모드가 없다
        ("aws", {"persistent": False}, False),  # emptyDir 은 PVC 가 아니다
        ("local", {"persistent": True}, False),
        ("local", {"persistent": True, "access_mode": "ReadWriteMany"}, True),
    ],
)
def test_sto001_volume_access_mode_must_be_supported(
    sample_app: dict[str, Any], env: str, volume: dict[str, Any], hit: bool
) -> None:
    vol = {"name": "uploads", "mount_path": "/u", "size": "1Gi", **volume}
    spec = {**sample_app, "storage": {"volumes": [vol]}, "target": {"env": env, "region": "r"}}
    found = findings_of(spec, "STO-001")
    assert bool(found) is hit
    if hit:
        assert (found[0]["autofix"], found[0]["location"]["spec_path"]) == ("forbidden", "/storage/volumes/0/access_mode")


@pytest.mark.parametrize(
    ("db", "hit"),
    [
        ({"engine": "postgres", "placement": "in-cluster", "version": "16"}, True),
        ({"engine": "sqlite", "placement": "volume", "volume": "data"}, True),
        ({"engine": "postgres", "placement": "managed", "version": "16"}, False),
    ],
    ids=["in-cluster", "sqlite-volume", "managed"],
)
def test_db002_aws_has_no_pvc_backed_database(sample_app: dict[str, Any], db: dict[str, Any], hit: bool) -> None:
    """2026-10-08 실측: EKS 에 EBS CSI 가 없어 PVC 를 쓰는 DB 배치는 AWS 에서 못 쓴다."""
    spec = {**sample_app, "database": db, "secrets": DB_SECRET}
    assert ("DB-002" in rule_ids(spec)) is hit


def test_run001_missing_readiness(sample_app: dict[str, Any]) -> None:
    sample_app["runtime"]["health"] = {"liveness": "/healthz"}
    f = findings_of(sample_app, "RUN-001")[0]
    assert (f["severity"], f["autofix"]) == ("medium", "forbidden")


def test_run004_multi_arch_image_is_fine(sample_app: dict[str, Any]) -> None:
    sample_app["image"]["platforms"] = ["arm64", "amd64"]
    assert "RUN-004" not in rule_ids(sample_app)


def _local(spec: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**spec, "target": {"env": "local", "region": "r"}, **changes}


UPLOADS = {"name": "uploads", "mount_path": "/uploads", "size": "1Gi"}


@pytest.mark.parametrize(
    ("volume", "replicas", "hit"),
    [
        ({}, 2, True),
        ({}, 1, False),
        ({"persistent": False}, 2, False),
        ({"access_mode": "ReadWriteMany"}, 2, False),
    ],
    ids=["rwo-replicated", "single", "ephemeral", "rwx"],
)
def test_sto002_rwo_volume_shared_by_replicas(sample_app: dict[str, Any], volume: dict, replicas: int, hit: bool) -> None:
    sample_app["runtime"]["replicas"] = replicas
    spec = _local(sample_app, storage={"volumes": [{**UPLOADS, **volume}]})
    found = findings_of(spec, "STO-002")
    assert bool(found) is hit
    if hit:
        assert (found[0]["autofix"], found[0]["location"]["spec_path"]) == ("allowed", "/storage/volumes/0")


def test_sto002_leaves_sqlite_volume_to_db003() -> None:
    spec = load_sample_dict("02-pass-local-sqlite.yaml")
    spec["runtime"]["replicas"] = 2
    assert "STO-002" not in rule_ids(spec) and "DB-003" in rule_ids(spec)


def test_sto006_removed_persistent_volume_points_into_baseline(sample_app: dict[str, Any]) -> None:
    scratch = {**UPLOADS, "name": "scratch", "persistent": False}
    prev = _local(sample_app, storage={"volumes": [UPLOADS, scratch]})
    spec = _local(sample_app, baseline={"spec_ref": "prev", "spec": prev})
    [f] = findings_of(spec, "STO-006")  # 비영속 볼륨을 뺀 것은 문제가 아니다
    assert (f["autofix"], f["irreversible"]) == ("forbidden", True)
    assert f["location"]["spec_path"] == "/baseline/spec/storage/volumes/0"
    assert "STO-006" not in rule_ids({**spec, "storage": {"volumes": [UPLOADS]}})


def test_sto006_turning_persistent_off_is_the_same_as_removing(sample_app: dict[str, Any]) -> None:
    """이름이 남아도 persistent 를 끄면 PVC 대신 emptyDir 이 붙는다 — STO-005 는 크기만 본다."""
    prev = _local(sample_app, storage={"volumes": [UPLOADS]})
    spec = _local(sample_app, storage={"volumes": [{**UPLOADS, "persistent": False}]},
                  baseline={"spec_ref": "prev", "spec": prev})
    [f] = findings_of(spec, "STO-006")
    assert (f["location"]["spec_path"], f["evidence"]) == ("/baseline/spec/storage/volumes/0",
                                                          "uploads (1Gi) persistent: false 로 바뀜")


def test_run002_missing_liveness_is_low(sample_app: dict[str, Any]) -> None:
    sample_app["runtime"]["health"] = {"readiness": "/healthz"}
    [f] = findings_of(sample_app, "RUN-002")
    assert (f["severity"], f["autofix"]) == ("low", "forbidden")


@pytest.mark.parametrize(
    ("resources", "evidence"),
    [({"cpu_limit": "250m"}, "memory_limit 없음"), ({}, "cpu_limit · memory_limit 없음")],
)
def test_run005_missing_resource_limits(sample_app: dict[str, Any], resources: dict, evidence: str) -> None:
    sample_app["runtime"]["resources"] = resources
    [f] = findings_of(sample_app, "RUN-005")
    assert (f["severity"], f["autofix"], f["evidence"]) == ("low", "allowed", evidence)


# ── 노드 ─────────────────────────────────────────────────────────


async def test_node_returns_only_findings() -> None:
    node = make_static_check()
    out = await node({"deploy_spec": load_sample_dict("07-fix-public-bucket.yaml"), "target_env": "aws"})
    assert list(out) == ["findings"]
    assert [f["rule_id"] for f in out["findings"]] == ["STO-003"]


# ── P1 나머지: DB-007·DB-008·SEC-002·SEC-003·NET-002·STO-004 ─────────────


@pytest.mark.parametrize(
    ("env", "db", "hit"),
    [
        ("aws", {**PG, "backup_retention_days": 0}, True),
        ("aws", PG, False),  # 기본값 1
        ("gcp", {"engine": "postgres", "version": "16", "placement": "in-cluster", "backup_retention_days": 0}, False),
        ("local", {**PG, "backup_retention_days": 0}, False),  # aws·gcp 규칙
    ],
)
def test_db007_managed_db_backup_off(sample_app: dict[str, Any], env: str, db: dict[str, Any], hit: bool) -> None:
    spec = {**sample_app, "database": db, "secrets": DB_SECRET, "target": {"env": env, "region": "r"}}
    found = findings_of(spec, "DB-007")
    assert bool(found) is hit
    if hit:
        assert (found[0]["severity"], found[0]["autofix"], found[0]["location"]["spec_path"]) == (
            "medium", "allowed", "/database/backup_retention_days")


@pytest.mark.parametrize(
    ("prev", "cur", "has_data", "hit"),
    [
        (PG, {**PG, "version": "15"}, True, True),
        (PG, {**PG, "version": "15"}, None, True),  # 모르면 데이터 있음
        (PG, {**PG, "version": "15"}, False, False),  # 데이터 없으면 새로 만들어도 된다
        ({**PG, "version": "15"}, PG, True, False),  # 업그레이드
        (PG, {**PG, "version": "16.4"}, True, False),  # 같은 메이저
        (MYSQL, {**MYSQL, "version": "5.7"}, True, True),
        (PG, {**PG, "version": None}, True, False),  # 버전을 모르면 판단하지 않는다
        (PG, MYSQL, True, False),  # 엔진 변경은 DB-001
    ],
)
def test_db008_major_version_downgrade(sample_app: dict[str, Any], prev: dict, cur: dict, has_data: bool | None,
                                       hit: bool) -> None:
    spec = _with_baseline({**sample_app, "database": cur, "secrets": DB_SECRET}, prev, has_data)
    found = findings_of(spec, "DB-008")
    assert bool(found) is hit
    if hit:
        f = found[0]
        assert (f["autofix"], f["irreversible"], f["location"]["spec_path"]) == ("forbidden", True, "/database/version")
        assert f["evidence"] == f"{cur['engine']} {prev['version']} → {cur['version']}"


def test_sec002_secret_source_must_exist_on_target(sample_app: dict[str, Any]) -> None:
    sample_app["secrets"] = [
        {"name": "A_KEY", "source": "k8s-secret", "key": "a"},
        {"name": "B_KEY", "source": "gcp-secret-manager", "key": "b"},
    ]
    [f] = findings_of(sample_app, "SEC-002")
    assert (f["autofix"], f["location"]["spec_path"]) == ("forbidden", "/secrets/1/source")
    sample_app["target"] = {"env": "gcp", "region": "r"}
    assert "SEC-002" not in rule_ids(sample_app)


def test_sec003_duplicate_secret_points_at_later_entries(sample_app: dict[str, Any]) -> None:
    first = {"name": "API_TOKEN", "source": "k8s-secret", "key": "token"}
    sample_app["secrets"] = [first, {"name": "OTHER", "source": "generated"}, first, {**first, "key": "token2"}]
    found = findings_of(sample_app, "SEC-003")
    assert [(f["autofix"], f["location"]["spec_path"]) for f in found] == [
        ("allowed", "/secrets/2"), ("allowed", "/secrets/3")]
    assert found[0]["evidence"] == "secrets[API_TOKEN] 중복 (처음: secrets/0)"


@pytest.mark.parametrize(
    ("ingress", "paths"),
    [
        ({"public": False, "allowed_cidrs": ["10.0.0.0/8", "0.0.0.0/0", "::/0"]},
         ["/network/ingress/allowed_cidrs/1", "/network/ingress/allowed_cidrs/2"]),
        ({"public": False, "allowed_cidrs": ["10.0.0.0/8"]}, []),
        ({"public": True, "allowed_cidrs": ["0.0.0.0/0"]}, []),  # 공개 진입점은 원래 전체 대역이다
    ],
)
def test_net002_internal_ingress_open_to_all(sample_app: dict[str, Any], ingress: dict, paths: list[str]) -> None:
    sample_app["network"]["ingress"] = ingress
    found = findings_of(sample_app, "NET-002")
    assert [f["location"]["spec_path"] for f in found] == paths
    assert all(f["autofix"] == "allowed" for f in found)


def test_sto004_unencrypted_bucket(sample_app: dict[str, Any]) -> None:
    sample_app["storage"] = {"buckets": [{"name": "uploads-a"}, {"name": "uploads-b", "encryption": False}]}
    [f] = findings_of(sample_app, "STO-004")
    assert (f["severity"], f["autofix"], f["location"]["spec_path"]) == ("medium", "allowed", "/storage/buckets/1/encryption")
    sample_app["target"] = {"env": "local", "region": "r"}
    assert "STO-004" not in rule_ids(sample_app)


# ── 온프레미스(local): DB-010 백업 없음 · NET-003 내부 전용 미강제 ───────────────


@pytest.mark.parametrize(
    ("env", "db", "persistence", "baseline_data", "hit"),
    [
        ("local", {"engine": "sqlite", "placement": "volume", "volume": "data"}, True, None, True),
        ("local", {**PG, "placement": "in-cluster"}, True, None, True),
        ("local", {**PG, "placement": "in-cluster", "backup_retention_days": 7}, True, None, True),  # 효과 없는 값
        ("local", {**PG, "placement": "in-cluster"}, False, None, False),  # 데이터를 남길 필요가 없다
        ("local", {**PG, "placement": "in-cluster"}, False, True, True),  # 선언이 없어도 이전 데이터가 있다
        ("local", {**PG, "placement": "external"}, True, None, False),  # 외부 DB 백업은 그쪽 운영 몫
        ("gcp", {**PG, "placement": "in-cluster"}, True, None, False),  # local 규칙
    ],
    ids=["sqlite-volume", "in-cluster-pg", "retention-ignored", "no-persistence", "baseline-data", "external", "gcp"],
)
def test_db010_self_hosted_db_without_backup(sample_app: dict[str, Any], env: str, db: dict[str, Any],
                                              persistence: bool, baseline_data: bool | None, hit: bool) -> None:
    spec = {**sample_app, "database": db, "secrets": DB_SECRET, "target": {"env": env, "region": "r"},
            "requirements": {"persistence": persistence},
            "storage": {"volumes": [{"name": "data", "mount_path": "/data", "size": "1Gi"}]}}
    if baseline_data is not None:
        spec["baseline"] = {"spec_ref": "prev", "facts": {"database_has_data": baseline_data}, "spec": copy.deepcopy(spec)}
    found = findings_of(spec, "DB-010")
    assert bool(found) is hit
    if hit:
        assert (found[0]["severity"], found[0]["autofix"], found[0]["location"]["spec_path"]) == (
            "low", "forbidden", "/database/placement")
        assert "자동 백업 없음" in found[0]["evidence"]


@pytest.mark.parametrize(
    ("env", "ingress", "hit"),
    [
        ("local", {"public": False}, True),
        ("local", {"public": False, "allowed_cidrs": ["192.168.0.0/16"]}, False),  # INGRESS_CIDRS_NOT_ENFORCED 가 막는다
        ("local", {"public": True, "tls": True, "host": "a.example.com"}, False),
        ("local", None, False),  # 밖으로 노출하지 않는다
        ("aws", {"public": False}, False),  # internal ALB 로 강제된다
        ("gcp", {"public": False}, False),  # gce-internal
    ],
    ids=["local-internal", "local-with-cidrs", "local-public", "local-no-ingress", "aws", "gcp"],
)
def test_net003_local_internal_not_enforced(sample_app: dict[str, Any], env: str, ingress: dict[str, Any] | None,
                                            hit: bool) -> None:
    spec = {**sample_app, "target": {"env": env, "region": "r"}, "network": {"ingress": ingress}}
    found = findings_of(spec, "NET-003")
    assert bool(found) is hit
    if hit:
        assert (found[0]["severity"], found[0]["autofix"], found[0]["location"]["spec_path"]) == (
            "low", "forbidden", "/network/ingress/public")
