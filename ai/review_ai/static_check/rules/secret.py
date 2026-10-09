from __future__ import annotations

from review_ai.secrets_pattern import MASK, looks_secret
from review_ai.static_check.context import CheckContext, Hit

DB_ENGINES_WITH_CREDENTIALS = frozenset({"postgres", "mysql"})


def plaintext_secret(ctx: CheckContext) -> list[Hit]:
    """SEC-001 — runtime.env 에 비밀로 보이는 평문. evidence 에는 이름만 남긴다."""
    return [
        Hit(f"/runtime/env/{name}", f"runtime.env.{name}={MASK}")
        for name, value in sorted(ctx.spec.runtime.env.items())
        if looks_secret(name, value)
    ]


def missing_db_secret(ctx: CheckContext) -> list[Hit]:
    """SEC-005 — postgres·mysql 인데 접속 정보 시크릿이 없다."""
    db = ctx.spec.database
    if db.engine not in DB_ENGINES_WITH_CREDENTIALS:
        return []
    if any(s.name == db.env_var for s in ctx.spec.secrets):
        return []
    return [Hit("/secrets", f"secrets 에 {db.env_var} 없음")]


def env_shadowed_by_secret(ctx: CheckContext) -> list[Hit]:
    """SEC-004 — 같은 이름이 runtime.env 와 secrets 에 둘 다 있다. 값은 비밀일 수 있어 evidence 에 남기지 않는다."""
    secret_names = {s.name for s in ctx.spec.secrets}
    return [
        Hit(f"/runtime/env/{name}", f"runtime.env.{name} 와 secrets[{name}] 중복")
        for name in sorted(ctx.spec.runtime.env)
        if name in secret_names
    ]


def unsupported_secret_source(ctx: CheckContext) -> list[Hit]:
    """SEC-002 — 대상 환경에 없는 비밀 저장소를 참조한다. 값을 동기화할 주체가 없어 Secret 키가 비어 Pod 가 뜨지 않는다."""
    supported = ctx.caps.secret_sources
    return [
        Hit(f"/secrets/{i}/source", f"{ctx.caps.env}: secrets[{s.name}] {s.source} 미지원 (지원: {sorted(supported)})")
        for i, s in enumerate(ctx.spec.secrets)
        if s.source not in supported
    ]


def duplicate_secret(ctx: CheckContext) -> list[Hit]:
    """SEC-003 — secrets[].name 이 겹친다. 위치는 두 번째부터의 항목이다 (첫 항목을 남긴다)."""
    first: dict[str, int] = {}
    hits = []
    for i, s in enumerate(ctx.spec.secrets):
        if s.name in first:
            hits.append(Hit(f"/secrets/{i}", f"secrets[{s.name}] 중복 (처음: secrets/{first[s.name]})"))
        else:
            first[s.name] = i
    return hits
