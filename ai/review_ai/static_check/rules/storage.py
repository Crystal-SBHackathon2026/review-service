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
