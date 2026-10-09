from __future__ import annotations

from review_ai.deploy_plan import is_breaking, mixed_versions_unsafe
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


def breaking_schema_change(ctx: CheckContext) -> list[Hit]:
    """RUN-006 — 옛 코드와 새 코드가 함께 돌 수 없는 스키마 변경을 한 번에 배포한다."""
    if not is_breaking(ctx.spec):
        return []
    observed = ctx.spec.observed
    source = f"새 SQL {', '.join(observed.migrations[:3])}" if observed and observed.schema_change == "breaking" else "선언"
    return [Hit("/database/migration/change", f"breaking · {source}")]


def canary_with_incompatible_versions(ctx: CheckContext) -> list[Hit]:
    """RUN-007 — 두 버전이 함께 돌면 안 되는데 canary 로 단계 배포한다."""
    if ctx.spec.rollout.strategy != "canary":
        return []
    previous = ctx.previous
    if previous is not None and ctx.has_existing_data and previous.database.engine != ctx.spec.database.engine:
        return []  # 데이터가 있는 엔진 변경은 DB-001 이 사람에게 넘긴다 — 이전 계획을 정할 때 전략도 같이 정한다
    reason = mixed_versions_unsafe(ctx.spec, ctx.previous)
    return [Hit("/rollout/strategy", f"canary · {reason}")] if reason else []


def migration_change_mismatch(ctx: CheckContext) -> list[Hit]:
    """RUN-008 — 새 마이그레이션 SQL 의 실제 변경과 선언(database.migration.change)이 다르다.

    선언이 breaking 이면 가장 보수적이라 맞춘다고 본다. 마이그레이션 명령이 없는데 SQL 이 늘었으면 실행할 Job 이 없다.
    """
    observed = ctx.spec.observed
    if observed is None or observed.schema_change == "none":
        return []
    migration = ctx.spec.database.migration
    files = ", ".join(observed.migrations[:3]) + (" …" if len(observed.migrations) > 3 else "")
    if migration is None:
        if ctx.spec.database.engine not in ("postgres", "mysql"):
            return []  # SQLite 는 앱이 시작할 때 마이그레이션한다
        return [Hit("/database/migration", f"새 SQL({files}) 이 {observed.schema_change} 인데 실행 명령이 없다")]
    if migration.change in (observed.schema_change, "breaking"):
        return []
    return [Hit("/database/migration/change", f"선언 {migration.change} · SQL {observed.schema_change} ({files})")]
