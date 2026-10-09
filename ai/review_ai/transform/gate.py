"""코드 패치 게이트 — LLM 이 만든 파일을 원래 파일·계획과 대조한다. 하나라도 어기면 패치 전체를 버린다.

| 코드 | 막는 것 |
|---|---|
| PATH_NOT_ALLOWED | 계획 밖 파일(Dockerfile·CI·deploy.yaml·잠금 파일·레포 밖 경로)을 쓰거나 지움 |
| TOO_LARGE | 파일 하나 60k 자·변경 15개 초과 |
| SECRET_LITERAL · MASKED_VALUE | 접속 문자열·토큰 같은 값을 코드에 씀 / 가린 값을 그대로 옮김 |
| DEPENDENCIES | 계획 밖 패키지 추가·삭제, 기존 버전·devDependencies·scripts 변경 |
| DRIVER_LEFT | 빼야 할 드라이버를 아직 불러온다 |
| ENV_MISSING | DB 접속을 DATABASE_URL 로 읽지 않는다 |
| ROUTES_REMOVED · METRICS_MISSING | 기존 HTTP 경로가 사라졌다 / /metrics 가 없다 |
| MIGRATION_MISSING · STARTUP_DDL | 테이블·세션 테이블·migrate 스크립트가 없다 / 앱 시작 코드에 CREATE TABLE 이 남았다 |

문법·실행은 확인하지 않는다 — 앱 CI 가 커밋에서 확인한다(코드를 검토 서비스에서 실행하지 않는다).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from review_ai.intake.analyze import _env_reads
from review_ai.secrets_pattern import MASK, has_secret_value
from review_ai.transform.plan import CREATE_TABLE, DATABASE_ENV, MIGRATE_SCRIPT, MIGRATIONS_DIR, TransformPlan
from review_ai.transform.prompt import TransformOutput

MAX_FILE_CHARS = 60_000
ROUTE = re.compile(r"\b(?:app|router)\.(get|post|put|patch|delete)\(\s*['\"`]([^'\"`]+)['\"`]")
SEMVER_RANGE = re.compile(r"^\^\d+(\.\d+){0,2}$")
SESSION_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?session[\"`]?\s*\(", re.IGNORECASE)
SOURCE_SUFFIX = ".js"


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


def _safe_path(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and "\\" not in path and ".." not in p.parts and path == str(p)


def _new_file_allowed(path: str) -> bool:
    p = PurePosixPath(path)
    return ((p.parts[0] == "src" and p.suffix == SOURCE_SUFFIX)
            or (p.parts[0] == MIGRATIONS_DIR and len(p.parts) == 2 and p.suffix == ".sql"))


def _imports(text: str, package: str) -> bool:
    return bool(re.search(rf"(?:require\(\s*|from\s+|import\s+)['\"]{re.escape(package)}['\"]", text))


def _routes(sources: Mapping[str, str]) -> set[tuple[str, str]]:
    return {(m.group(1), m.group(2)) for text in sources.values() for m in ROUTE.finditer(text)}


def _package(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _check_package(before: str, after: str, plan: TransformPlan) -> list[Issue]:
    old, new = _package(before), _package(after)
    if old is None or new is None:
        return [Issue("DEPENDENCIES", "package.json", "JSON 으로 읽을 수 없다")]
    issues: list[Issue] = []
    adds = set().union(*(i.add_dependencies for i in plan.items))
    removes = set().union(*(i.remove_dependencies for i in plan.items))
    old_deps, new_deps = old.get("dependencies") or {}, new.get("dependencies") or {}
    if extra := sorted(set(new_deps) - set(old_deps) - adds):
        issues.append(Issue("DEPENDENCIES", "package.json", f"계획에 없는 패키지 추가: {', '.join(extra)}"))
    if left := sorted(removes & set(new_deps)):
        issues.append(Issue("DEPENDENCIES", "package.json", f"빼야 할 패키지가 남았다: {', '.join(left)}"))
    if dropped := sorted(set(old_deps) - set(new_deps) - removes):
        issues.append(Issue("DEPENDENCIES", "package.json", f"계획 밖 패키지 삭제: {', '.join(dropped)}"))
    if changed := sorted(n for n in set(old_deps) & set(new_deps) if old_deps[n] != new_deps[n]):
        issues.append(Issue("DEPENDENCIES", "package.json", f"기존 패키지 버전 변경: {', '.join(changed)}"))
    if bad := sorted(n for n in set(new_deps) - set(old_deps) if not SEMVER_RANGE.match(str(new_deps[n]))):
        issues.append(Issue("DEPENDENCIES", "package.json", f"버전은 ^메이저.마이너.패치 범위만: {', '.join(bad)}"))
    allowed_scripts = {"migrate": f"node {MIGRATE_SCRIPT}"} if "DB_SQLITE_TO_POSTGRES" in plan.codes else {}
    old_scripts, new_scripts = old.get("scripts") or {}, new.get("scripts") or {}
    for name in set(old_scripts) | set(new_scripts):
        if old_scripts.get(name) != new_scripts.get(name) and new_scripts.get(name) != allowed_scripts.get(name):
            issues.append(Issue("DEPENDENCIES", "package.json", f"scripts.{name} 를 바꿀 수 없다"))
    others = {k for k in set(old) | set(new) if k not in ("dependencies", "scripts") and old.get(k) != new.get(k)}
    if others:
        issues.append(Issue("DEPENDENCIES", "package.json", f"다른 항목 변경: {', '.join(sorted(others))}"))
    return issues


def _check_db(plan: TransformPlan, sources: Mapping[str, str], migrations: Mapping[str, str]) -> list[Issue]:
    issues: list[Issue] = []
    removes = set().union(*(i.remove_dependencies for i in plan.items))
    for path, text in sources.items():
        if left := [n for n in sorted(removes) if _imports(text, n)]:
            issues.append(Issue("DRIVER_LEFT", path, f"아직 불러온다: {', '.join(left)}"))
    if not any(DATABASE_ENV in _env_reads(text)[0] for p, text in sources.items() if p != MIGRATE_SCRIPT):
        issues.append(Issue("ENV_MISSING", "src/", f"앱이 DB 접속을 {DATABASE_ENV} 로 읽지 않는다"))
    script = sources.get(MIGRATE_SCRIPT, "")
    if DATABASE_ENV not in _env_reads(script)[0] or "schema_migrations" not in script:
        issues.append(Issue("MIGRATION_MISSING", MIGRATE_SCRIPT,
                            f"{DATABASE_ENV} 로 접속해 schema_migrations 에 기록하는 migrate 스크립트가 없다"))
    sql = "\n".join(migrations.values())
    created = {m.group(1).lower() for m in CREATE_TABLE.finditer(sql)}
    if missing := [t for t in plan.tables if t not in created]:
        issues.append(Issue("MIGRATION_MISSING", MIGRATIONS_DIR, f"테이블을 만들지 않는다: {', '.join(missing)}"))
    if not SESSION_TABLE.search(sql):
        issues.append(Issue("MIGRATION_MISSING", MIGRATIONS_DIR, "connect-pg-simple 세션 테이블(session)을 만들지 않는다"))
    for path, text in sources.items():
        if path != MIGRATE_SCRIPT and CREATE_TABLE.search(text):
            issues.append(Issue("STARTUP_DDL", path, "앱 코드에 CREATE TABLE 이 남았다 — 마이그레이션 Job 이 맡는다"))
    return issues


def check_patch(plan: TransformPlan, output: TransformOutput, originals: Mapping[str, str],
                sources: Mapping[str, str]) -> tuple[dict[str, str | None], list[Issue]]:
    """originals: 고칠 수 있는 원래 파일(소스·package.json). sources: 원래 소스 전부(경로 비교용).

    돌려주는 것: 경로 → 새 내용(None = 삭제), 위반 목록. 위반이 하나라도 있으면 쓰지 않는다.
    """
    issues: list[Issue] = []
    changes: dict[str, str | None] = {}
    removes = set().union(*(i.remove_dependencies for i in plan.items))
    if len(output.changes) > len({c.path for c in output.changes}):
        issues.append(Issue("PATH_NOT_ALLOWED", "", "같은 파일을 두 번 바꿨다"))
    for change in output.changes:
        path = change.path
        if not _safe_path(path) or not (path in originals or _new_file_allowed(path)):
            issues.append(Issue("PATH_NOT_ALLOWED", path, "계획 밖 파일이다"))
            continue
        if change.action == "delete":
            original = originals.get(path, "")
            # 드라이버를 불러오는 파일, 또는 SQLite 전용 모듈(드라이버 객체를 받아 쓰는 세션 저장소 등)만 지운다
            if not (any(_imports(original, n) for n in removes) or "sqlite" in (path + original).lower()):
                issues.append(Issue("PATH_NOT_ALLOWED", path, "빼야 할 패키지를 쓰지 않는 파일은 지울 수 없다"))
            changes[path] = None
            continue
        if len(change.content) > MAX_FILE_CHARS:
            issues.append(Issue("TOO_LARGE", path, f"{MAX_FILE_CHARS}자를 넘는다"))
        if MASK in change.content:
            issues.append(Issue("MASKED_VALUE", path, "가린 값을 코드에 옮겼다"))
        elif any(has_secret_value(line) for line in change.content.splitlines()):
            issues.append(Issue("SECRET_LITERAL", path, "비밀번호·토큰 같은 값을 코드에 썼다"))
        changes[path] = change.content
    if issues:
        return changes, issues

    after = {p: t for p, t in {**originals, **changes}.items() if t is not None}
    if "package.json" in changes:
        issues += _check_package(originals["package.json"], after["package.json"], plan)
    elif any(i.add_dependencies or i.remove_dependencies for i in plan.items):
        issues.append(Issue("DEPENDENCIES", "package.json", "계획한 패키지 변경이 없다"))
    new_sources = {p: t for p, t in {**sources, **changes}.items()
                   if t is not None and PurePosixPath(p).suffix == SOURCE_SUFFIX}
    if lost := sorted(_routes(sources) - _routes(new_sources)):
        issues.append(Issue("ROUTES_REMOVED", "", "사라진 경로: " + ", ".join(f"{m.upper()} {p}" for m, p in lost)))
    if "METRICS_ENDPOINT" in plan.codes and ("get", "/metrics") not in _routes(new_sources):
        issues.append(Issue("METRICS_MISSING", "", "GET /metrics 가 없다"))
    if "DB_SQLITE_TO_POSTGRES" in plan.codes:
        migrations = {p: t for p, t in after.items() if PurePosixPath(p).parts[0] == MIGRATIONS_DIR}
        issues += _check_db(plan, new_sources, migrations)
    return changes, issues
