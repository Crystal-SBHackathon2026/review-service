from __future__ import annotations

from review_ai.static_check.context import CheckContext, Hit


def missing_readiness(ctx: CheckContext) -> list[Hit]:
    """RUN-001 — readiness 경로가 없다. Rollout 자동 중단이 이걸로 실패를 판정한다."""
    if ctx.spec.runtime.health.readiness:
        return []
    return [Hit("/runtime/health/readiness", "readiness 없음")]


def arch_mismatch(ctx: CheckContext) -> list[Hit]:
    """RUN-004 — 이미지 플랫폼과 대상 노드 아키텍처가 겹치지 않는다."""
    platforms = set(ctx.spec.image.platforms)
    if platforms & ctx.caps.arch:
        return []
    return [Hit("/image/platforms", f"이미지 {sorted(platforms)} · 노드 {sorted(ctx.caps.arch)}")]


def missing_liveness(ctx: CheckContext) -> list[Hit]:
    """RUN-002 — liveness 경로가 없다. 멈춘 프로세스를 kubelet 이 재시작하지 못한다."""
    if ctx.spec.runtime.health.liveness:
        return []
    return [Hit("/runtime/health/liveness", "liveness 없음")]


def missing_resource_limits(ctx: CheckContext) -> list[Hit]:
    """RUN-005 — cpu_limit·memory_limit 중 비어 있는 것이 있다."""
    resources = ctx.spec.runtime.resources
    missing = [name for name in ("cpu_limit", "memory_limit") if getattr(resources, name) is None]
    if not missing:
        return []
    return [Hit("/runtime/resources", f"{' · '.join(missing)} 없음")]
