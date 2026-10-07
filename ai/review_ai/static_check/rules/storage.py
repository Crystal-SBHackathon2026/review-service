from __future__ import annotations

from review_ai.static_check.context import CheckContext, Hit, size_mi


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
