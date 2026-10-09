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
    """DB-004 — 재배포 뒤에도 데이터가 남아야 하는데 DB 도 persistent 볼륨도 없다."""
    spec = ctx.spec
    if not spec.requirements.persistence or spec.database.engine != "none":
        return []
    if any(v.persistent for v in spec.storage.volumes):
        return []
    return [Hit("/requirements/persistence", "persistence: true · database.engine none · persistent 볼륨 없음")]


def managed_db_public(ctx: CheckContext) -> list[Hit]:
    """DB-006 — 관리형 DB 가 인터넷에 열려 있다."""
    db = ctx.spec.database
    if db.placement != "managed" or not db.publicly_accessible:
        return []
    return [Hit("/database/publicly_accessible", f"managed {db.engine}: publicly_accessible true")]
