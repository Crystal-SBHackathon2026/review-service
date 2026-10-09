from __future__ import annotations

from review_ai.static_check.context import CheckContext, Hit, size_mi


def unsupported_access_mode(ctx: CheckContext) -> list[Hit]:
    """STO-001 — persistent 볼륨의 접근 모드를 대상 환경이 지원하지 않는다 (지원 목록이 비면 볼륨 자체를 못 만든다)."""
    supported = ctx.caps.volume_access_modes
    return [
        Hit(f"/storage/volumes/{i}/access_mode", f"{ctx.caps.env}: {v.name} {v.access_mode} 미지원 (지원: {sorted(supported) or '없음'})")
        for i, v in enumerate(ctx.spec.storage.volumes)
        if v.persistent and v.access_mode not in supported
    ]


def public_bucket(ctx: CheckContext) -> list[Hit]:
    """STO-003 — 버킷이 공개돼 있다."""
    return [
        Hit(f"/storage/buckets/{i}/public", f"bucket {b.name}: public")
        for i, b in enumerate(ctx.spec.storage.buckets)
        if b.public
    ]


def unencrypted_bucket(ctx: CheckContext) -> list[Hit]:
    """STO-004 — 버킷 암호화가 꺼져 있다."""
    return [
        Hit(f"/storage/buckets/{i}/encryption", f"bucket {b.name}: encryption false")
        for i, b in enumerate(ctx.spec.storage.buckets)
        if not b.encryption
    ]


def volume_shrink(ctx: CheckContext) -> list[Hit]:
    """STO-005 — 이전 배포의 persistent 볼륨보다 작다. PVC 는 줄일 수 없다."""
    if ctx.previous is None:
        return []
    before = {v.name: v for v in ctx.previous.storage.volumes if v.persistent}
    hits = []
    for i, vol in enumerate(ctx.spec.storage.volumes):
        prev = before.get(vol.name)
        if prev is not None and size_mi(vol.size) < size_mi(prev.size):
            hits.append(Hit(f"/storage/volumes/{i}/size", f"{vol.name}: {prev.size} → {vol.size}"))
    return hits


def rwo_volume_replicated(ctx: CheckContext) -> list[Hit]:
    """STO-002 — DB 용이 아닌 persistent RWO 볼륨을 여러 replica 가 쓴다 (SQLite 볼륨은 DB-003 이 맡는다)."""
    replicas = ctx.spec.runtime.replicas
    if replicas <= 1:
        return []
    db = ctx.spec.database
    db_volume = db.volume if db.engine == "sqlite" else None
    return [
        Hit(f"/storage/volumes/{i}", f"{v.name}: ReadWriteOnce + replicas {replicas}")
        for i, v in enumerate(ctx.spec.storage.volumes)
        if v.persistent and v.access_mode == "ReadWriteOnce" and v.name != db_volume
    ]


def persistent_volume_removed(ctx: CheckContext) -> list[Hit]:
    """STO-006 — 이전 배포의 persistent 볼륨이 지금 명세에 없거나 persistent 가 꺼졌다. 둘 다 PVC 가 렌더되지 않아 데이터가 사라진다.

    지운 볼륨은 지금 명세에 자리가 없으므로 위치는 둘 다 baseline 안의 이전 볼륨을 가리킨다.
    """
    if ctx.previous is None:
        return []
    current = {v.name: v for v in ctx.spec.storage.volumes}
    hits = []
    for i, prev in enumerate(ctx.previous.storage.volumes):
        now = current.get(prev.name)
        if not prev.persistent or (now is not None and now.persistent):
            continue
        change = "제거" if now is None else "persistent: false 로 바뀜"
        hits.append(Hit(f"/baseline/spec/storage/volumes/{i}", f"{prev.name} ({prev.size}) {change}"))
    return hits
