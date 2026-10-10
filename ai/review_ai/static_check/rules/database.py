from __future__ import annotations

from review_ai.static_check.context import CheckContext, Hit


def engine_change_with_data(ctx: CheckContext) -> list[Hit]:
    """DB-001 — 데이터가 있는(또는 모르는) DB 의 엔진을 바꾼다."""
    if ctx.previous is None or not ctx.has_existing_data:
        return []
    before, after = ctx.previous.database.engine, ctx.spec.database.engine
    if before == after:
        return []
    return [Hit("/database/engine", f"{before} → {after}")]


def unsupported_engine(ctx: CheckContext) -> list[Hit]:
    """DB-002 — (placement, engine, version) 이 대상 환경 능력표에 없다."""
    db = ctx.spec.database
    if db.engine == "none" or db.placement is None:
        return []
    if ctx.caps.supports_db(db.placement, db.engine, db.version):
        return []
    version = f" {db.version}" if db.version else ""
    return [Hit("/database", f"{ctx.caps.env}: {db.placement} {db.engine}{version} 미지원")]


def sqlite_replicated(ctx: CheckContext) -> list[Hit]:
    """DB-003 — SQLite 파일을 여러 replica 가 공유한다."""
    if ctx.spec.database.engine != "sqlite" or ctx.spec.runtime.replicas <= 1:
        return []
    return [Hit("/runtime/replicas", f"sqlite + replicas {ctx.spec.runtime.replicas}")]


def sqlite_without_persistent_volume(ctx: CheckContext) -> list[Hit]:
    """DB-005 — 영속성이 필요한 SQLite 가 영속 볼륨 밖에 있다."""
    db = ctx.spec.database
    if db.engine != "sqlite" or not ctx.spec.requirements.persistence:
        return []
    volume = next((v for v in ctx.spec.storage.volumes if v.name == db.volume), None)
    if volume is not None and volume.persistent:
        return []
    state = "없음" if volume is None else "persistent: false"
    return [Hit("/database/volume", f"database.volume={db.volume!r} ({state})")]


def persistence_without_storage(ctx: CheckContext) -> list[Hit]:
    """DB-004 — 재배포 뒤에도 데이터가 남아야 하는데 DB·persistent 볼륨·버킷 어디에도 둘 곳이 없다."""
    spec = ctx.spec
    if not spec.requirements.persistence or spec.database.engine != "none":
        return []
    if spec.storage.buckets or any(v.persistent for v in spec.storage.volumes):
        return []
    return [Hit("/requirements/persistence", "persistence: true · database.engine none · persistent 볼륨·버킷 없음")]


def managed_db_public(ctx: CheckContext) -> list[Hit]:
    """DB-006 — 관리형 DB 가 인터넷에 열려 있다."""
    db = ctx.spec.database
    if db.placement != "managed" or not db.publicly_accessible:
        return []
    return [Hit("/database/publicly_accessible", f"managed {db.engine}: publicly_accessible true")]


def managed_db_without_backup(ctx: CheckContext) -> list[Hit]:
    """DB-007 — 관리형 DB 의 자동 백업이 꺼져 있다."""
    db = ctx.spec.database
    if db.placement != "managed" or db.backup_retention_days > 0:
        return []
    return [Hit("/database/backup_retention_days", f"managed {db.engine}: backup_retention_days 0")]



# 백업을 플랫폼이 만들어 주지 않는 배치 — PVC 위 파일·Pod 가 데이터의 유일한 사본이다
SELF_HOSTED_PLACEMENTS = ("in-cluster", "volume")


def self_hosted_db_without_backup(ctx: CheckContext) -> list[Hit]:
    """DB-010 — 클러스터 안 DB(in-cluster postgres·볼륨 SQLite)에 백업 수단이 없다 (low 경고).

    DB-007 은 관리형만 본다. 여기는 backup_retention_days 가 무엇이든 효과가 없다 — overlay 가 백업을 만들지 않는다.
    """
    db = ctx.spec.database
    if db.engine == "none" or db.placement not in SELF_HOSTED_PLACEMENTS:
        return []
    if not (ctx.spec.requirements.persistence or ctx.has_existing_data):
        return []
    return [Hit("/database/placement",
                f"{ctx.caps.env}: {db.placement} {db.engine} — 자동 백업 없음 (backup_retention_days 는 managed 에만 적용)")]

def _version_key(version: str | None) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in version.split(".")) if version else None
    except ValueError:
        return None


def version_downgrade(ctx: CheckContext) -> list[Hit]:
    """DB-008 — 데이터가 있는(또는 모르는) DB 의 메이저 버전을 낮춘다. 엔진이 바뀌면 DB-001 이 맡는다.

    버전은 짧은 쪽 길이까지만 비교한다 — '16' 과 '16.4' 는 같은 메이저 버전이다. 숫자가 아니면 판단하지 않는다.
    """
    if ctx.previous is None or not ctx.has_existing_data:
        return []
    before, after = ctx.previous.database, ctx.spec.database
    if before.engine != after.engine:
        return []
    old, new = _version_key(before.version), _version_key(after.version)
    if old is None or new is None:
        return []
    width = min(len(old), len(new))
    if new[:width] >= old[:width]:
        return []
    return [Hit("/database/version", f"{after.engine} {before.version} → {after.version}")]


def sqlite_data_left_behind(ctx: CheckContext) -> list[Hit]:
    """DB-009 — 데이터가 있는 SQLite 에서 다른 엔진으로 바꾸는데 옮길 계획(database.data_import)이 없다."""
    previous = ctx.previous
    if previous is None or not ctx.has_existing_data or previous.database.engine != "sqlite":
        return []
    if ctx.spec.database.engine == "sqlite" or ctx.spec.database.data_import is not None:
        return []
    return [Hit("/database/data_import", f"sqlite(볼륨 {previous.database.volume}) → {ctx.spec.database.engine}, 이전 계획 없음")]
