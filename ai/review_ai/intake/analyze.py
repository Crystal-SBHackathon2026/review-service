"""레포 파일로 새 앱의 생성 문맥을 채운다 — LLM 없이 규칙으로, 근거가 분명한 값만.

입력은 PR head 의 파일 경로 목록과 읽은 파일 원문이다 (Review API 가 GitHub 에서 읽어 넘긴다). 네트워크 없음.
채운 값마다 근거(파일·이유)를 남기고, 하나라도 애매하면 그 항목은 비워 둔다 → prepare_spec 이 확인 항목으로 남겨
커밋하지 않는다. '없다'는 결론(DB 없음·시크릿 없음·저장소 없음)은 의존성 파일과 소스를 다 읽었을 때만 낸다 —
분석하지 않는 언어(Java·Ruby·셸 …)나 하위 디렉터리·다른 형식의 의존성 파일이 있으면 내지 않는다.

| 항목 | 근거 |
|---|---|
| image | CI 워크플로의 ghcr.io 이미지 경로, platforms·--platform (없으면 ubuntu 러너 = amd64) |
| runtime | Dockerfile 최종 스테이지 EXPOSE(하나), HEALTHCHECK 의 http://localhost:<그 포트>/경로 — 없으면 소스의 /readyz·/livez·/healthz 같은 헬스 라우트 |
| database | package.json·requirements.txt·pyproject.toml·go.mod 의 DB 드라이버. 드라이버가 있으면 배치를 몰라 비워 둔다 |
| secrets | 소스가 읽는 환경변수 이름(구조 분해 포함) — 비밀로 보이는 이름이 있으면 어디서 읽을지 몰라 비워 둔다. 세션·쿠키·CSRF·JWT 서명 키는 generated |
| storage | Dockerfile VOLUME, 파일 쓰기 호출, 업로드·오브젝트 스토리지 의존성 |
| requirements | DB 없음 + 저장소 없음일 때만 persistence=false |
| smoke | 테스트 파일이 하나도 없을 때만 — readiness 경로 + 소스의 헬스·정보성 고정 GET 라우트 (Express·FastAPI·Flask·net/http·gin) |
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from review_ai.preparation import GenerationContext
from review_ai.secrets_pattern import is_secret_name
from review_ai.spec.deploy_spec import (
    BASE_RESOURCES, Database, Health, Image, Requirements, Runtime, SecretRef, Smoke, Storage,
)

MANIFESTS = ("package.json", "requirements.txt", "pyproject.toml", "go.mod")
SOURCE_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".ts", ".mts", ".py", ".go"})
SKIP_DIRS = frozenset({"node_modules", "vendor", "dist", "build", "public", "static", "assets",
                       "test", "tests", "__tests__", "spec", "docs",
                       "examples", "scripts", ".github", "migrations"})
TEST_NAME = re.compile(r"(\.(test|spec)\.[cm]?[jt]s|_test\.go|^test_.*\.py|_test\.py)$")
MAX_SOURCE_FILES = 40
# 이 파일이 있으면 소스·의존성을 다 읽었다고 할 수 없다 — '없음' 결론을 내지 않는다
UNSCANNED_SUFFIXES = frozenset({".java", ".kt", ".kts", ".scala", ".groovy", ".rb", ".php", ".rs", ".cs", ".fs",
                                ".ex", ".exs", ".erl", ".clj", ".swift", ".dart", ".c", ".cc", ".cpp", ".lua", ".pl",
                                ".sh", ".csproj"})
OTHER_MANIFESTS = frozenset({"Pipfile", "setup.py", "setup.cfg", "Gemfile", "pom.xml", "build.gradle",
                             "build.gradle.kts", "Cargo.toml", "composer.json", "mix.exs", "deno.json"})

# 생태계:이름 → 엔진. go 는 모듈 경로 접두로 맞춘다 (pgx/v5 등).
DRIVERS: dict[str, str] = {
    "npm:pg": "postgres", "npm:postgres": "postgres", "npm:pg-promise": "postgres",
    "npm:mysql": "mysql", "npm:mysql2": "mysql",
    "npm:sqlite3": "sqlite", "npm:better-sqlite3": "sqlite", "npm:sqlite": "sqlite",
    "py:psycopg": "postgres", "py:psycopg2": "postgres", "py:psycopg2-binary": "postgres",
    "py:psycopg-binary": "postgres", "py:asyncpg": "postgres", "py:pg8000": "postgres",
    "py:pymysql": "mysql", "py:mysqlclient": "mysql", "py:aiomysql": "mysql", "py:mysql-connector-python": "mysql",
    "py:aiosqlite": "sqlite",
    "go:github.com/lib/pq": "postgres", "go:github.com/jackc/pgx": "postgres",
    "go:github.com/go-sql-driver/mysql": "mysql",
    "go:github.com/mattn/go-sqlite3": "sqlite", "go:modernc.org/sqlite": "sqlite",
}
# ORM·다른 저장소 — 엔진·데이터 보존을 정할 수 없다
STATEFUL = frozenset({
    "npm:sequelize", "npm:typeorm", "npm:knex", "npm:prisma", "npm:@prisma/client", "npm:drizzle-orm",
    "npm:@mikro-orm/core", "npm:mongoose", "npm:mongodb", "npm:redis", "npm:ioredis",
    "py:sqlalchemy", "py:django", "py:peewee", "py:tortoise-orm", "py:sqlmodel", "py:pymongo", "py:motor",
    "py:redis",
    "go:gorm.io/gorm", "go:github.com/jmoiron/sqlx", "go:go.mongodb.org/mongo-driver", "go:github.com/redis/go-redis",
})
# 환경변수를 이름 패턴 없이 읽는 설정 라이브러리 — 필요한 시크릿을 소스에서 알 수 없다
ENV_LIBS = frozenset({
    "npm:convict", "npm:config", "npm:nconf", "npm:env-var", "npm:@nestjs/config",
    "py:python-decouple", "py:environs", "py:django-environ", "py:pydantic-settings", "py:dynaconf",
    "go:github.com/kelseyhightower/envconfig", "go:github.com/caarlos0/env", "go:github.com/spf13/viper",
    "go:github.com/knadh/koanf",
})
STORAGE_DEPS = frozenset({
    "npm:multer", "npm:formidable", "npm:busboy", "npm:aws-sdk", "npm:@aws-sdk/client-s3",
    "npm:@google-cloud/storage", "npm:minio",
    "py:boto3", "py:google-cloud-storage", "py:minio",
    "go:cloud.google.com/go/storage", "go:github.com/aws/aws-sdk-go", "go:github.com/aws/aws-sdk-go-v2",
    "go:github.com/minio/minio-go",
})

ENV_READS = (
    re.compile(r"process\.env\.([A-Za-z_]\w*)"),
    re.compile(r"process\.env\[\s*['\"](\w+)['\"]\s*\]"),
    re.compile(r"os\.environ\[\s*['\"](\w+)['\"]\s*\]"),
    re.compile(r"os\.environ\.get\(\s*['\"](\w+)['\"]"),
    re.compile(r"os\.getenv\(\s*['\"](\w+)['\"]"),
    re.compile(r"os\.(?:Getenv|LookupEnv)\(\s*\"(\w+)\""),
)
# const { A, B: b, C = 'x' } = process.env — 이름이 코드에 있는 읽기. ...rest 는 이름을 모른다
ENV_DESTRUCTURE = re.compile(r"(?:const|let|var)\s*\{([^{}]*)\}\s*=\s*process\.env\b(?!\s*[.\[])")
# 배포마다 무작위로 만들어도 되는 앱 내부 서명 키 — 외부 서비스 키(STRIPE_SECRET_KEY 등)는 사람이 값을 정한다
GENERATABLE_SECRET = re.compile(r"(?:SESSION|COOKIE|CSRF|JWT)_(?:SECRET|KEY)")
# 이름을 코드에서 알 수 없는 읽기 — process.env 통째로 넘기기, 변수 키, 설정 클래스
DYNAMIC_ENV = re.compile(
    r"process\.env(?![.\[\w])|process\.env\[\s*[^'\"\s]|os\.environ(?!\s*\[\s*['\"]|\.get\(\s*['\"]|\w)"
    r"|os\.environ\[\s*[^'\"\s]|os\.getenv\(\s*[^'\"\s)]|os\.(?:Getenv|LookupEnv)\(\s*[^\"\s)]|os\.Environ\(\)"
    r"|BaseSettings")
FILE_WRITES = re.compile(
    r"\b(?:writeFile(?:Sync)?|appendFile(?:Sync)?|createWriteStream|mkdirSync)\(|\bfs\.(?:mkdir|rename|copyFile)\("
    r"|\bopen\([^)\n]*,\s*['\"][wax]|\bmode\s*=\s*['\"][wax]|\.open\(\s*['\"][wax]|\.write_(?:text|bytes)\(|\bshutil\.(?:copy\w*|move)\("
    r"|\bos\.(?:Create|WriteFile|OpenFile|Mkdir|MkdirAll)\(|\bioutil\.WriteFile\(")
SQLITE_STDLIB = re.compile(r"['\"]node:sqlite['\"]|^\s*(?:import|from)\s+sqlite3\b", re.MULTILINE)
GO_SQL = re.compile(r"\"database/sql\"")
LOCAL_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::(\d+))?(/[A-Za-z0-9._~/-]*)?")
GHCR = re.compile(r"ghcr\.io/[^\s\"'\],]+")
IMAGE_PATH = re.compile(r"ghcr\.io(?:/[a-z0-9._-]+){2,}")
PLATFORM_LINE = re.compile(r"(?:platforms?:|--platform[= ])([^\n]+)")
LINUX_ARCH = re.compile(r"linux/(amd64|arm64)")
RUNS_ON = re.compile(r"runs-on:\s*([^\n]+)")
TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec"})
# 고정 경로의 GET 라우트만 — :id·{id}·* 처럼 값이 들어가는 경로는 문자 집합에서 빠진다
_ROUTE_PATH = r"(/[A-Za-z0-9._~/-]*)"
GET_ROUTES = (
    re.compile(rf"\b(?:app|router|server)\.get\(\s*['\"`]{_ROUTE_PATH}['\"`]"),  # Express
    re.compile(rf"@(?:app|router|api)\.get\(\s*['\"]{_ROUTE_PATH}['\"]"),  # FastAPI
    re.compile(rf"@(?:app|bp|blueprint)\.route\(\s*['\"]{_ROUTE_PATH}['\"]\s*\)"),  # Flask (GET 기본)
    re.compile(rf"\bHandleFunc\(\s*\"(?:GET\s+)?{_ROUTE_PATH}\""),  # net/http
    re.compile(rf"\.GET\(\s*\"{_ROUTE_PATH}\""),  # gin·echo
)
# 확인용으로 GET 해도 되는 경로 — 로그인·입력이 필요한 업무 경로(/api/todos 등)는 401·400 이라 확인에 쓰지 않는다
PROBE_LIKE = re.compile(r"(?:^|/)(?:healthz?|livez?|liveness|readyz?|readiness|ping|status|version|info)$")
MAX_SMOKE_PATHS = 5


@dataclass(frozen=True)
class Finding:
    """채웠거나(resolved) 못 채운 항목 하나와 그 근거."""

    path: str        # /runtime 등 명세 경로
    source: str      # 근거 파일 — Dockerfile, package.json, 소스 3개 …
    reason: str
    resolved: bool

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "source": self.source, "reason": self.reason}


@dataclass(frozen=True)
class RepoAnalysis:
    context: GenerationContext
    findings: tuple[Finding, ...]

    @property
    def unresolved(self) -> dict[str, Finding]:
        return {f.path: f for f in self.findings if not f.resolved}


@dataclass(frozen=True)
class _Docker:
    ports: tuple[int, ...]
    odd_ports: tuple[str, ...]      # $PORT 처럼 숫자가 아닌 EXPOSE
    healthcheck: str | None
    volumes: tuple[str, ...]
    env_names: tuple[str, ...]


@dataclass(frozen=True)
class _Deps:
    names: frozenset[str]
    manifests: tuple[str, ...]
    problems: tuple[str, ...]


# --- 읽을 파일 고르기 -------------------------------------------------------------------------------

def _is_workflow(path: str) -> bool:
    p = PurePosixPath(path)
    return p.parent == PurePosixPath(".github/workflows") and p.suffix in (".yml", ".yaml")


def _source_files(tree: Iterable[str]) -> list[str]:
    def wanted(path: str) -> bool:
        p = PurePosixPath(path)
        return (p.suffix in SOURCE_SUFFIXES and not p.name.endswith((".d.ts", ".min.js"))
                and not TEST_NAME.search(p.name)
                and not SKIP_DIRS.intersection(p.parts[:-1]))
    return sorted(p for p in tree if wanted(p))


def _unscanned(tree: Iterable[str]) -> list[str]:
    """분석하지 않는 언어의 소스, 하위 디렉터리·다른 형식의 의존성 파일."""
    def gap(path: str) -> bool:
        p = PurePosixPath(path)
        if SKIP_DIRS.intersection(p.parts[:-1]):
            return False
        return (p.name in OTHER_MANIFESTS or p.suffix in UNSCANNED_SUFFIXES
                or (p.name in MANIFESTS and len(p.parts) > 1))
    return sorted(p for p in tree if gap(p))


def files_to_read(tree: Iterable[str]) -> tuple[str, ...]:
    """분석에 필요한 파일 — 루트 Dockerfile·의존성 파일, CI 워크플로, 소스(최대 MAX_SOURCE_FILES 개)."""
    paths = set(tree)
    root = [p for p in ("Dockerfile", *MANIFESTS) if p in paths]
    workflows = sorted(p for p in paths if _is_workflow(p))
    return (*root, *workflows, *_source_files(paths)[:MAX_SOURCE_FILES])


# --- 파일 해석 --------------------------------------------------------------------------------------

def _dockerfile(text: str) -> _Docker:
    lines = re.sub(r"\\\r?\n", " ", text).splitlines()
    instructions = [(parts[0].upper(), parts[1] if len(parts) > 1 else "")
                    for line in lines if (stripped := line.strip()) and not stripped.startswith("#")
                    for parts in [stripped.split(None, 1)]]
    last_from = max((i for i, (word, _) in enumerate(instructions) if word == "FROM"), default=-1)
    final = instructions[last_from + 1:]  # 멀티 스테이지면 최종 스테이지만 실행된다
    ports: list[int] = []
    odd: list[str] = []
    for word, rest in final:
        if word != "EXPOSE":
            continue
        for token in rest.split():
            m = re.fullmatch(r"(\d+)(?:/(tcp|udp))?", token, re.IGNORECASE)
            if m is None:
                odd.append(token)
            elif (m.group(2) or "tcp").lower() == "tcp":
                ports.append(int(m.group(1)))
    health = [rest for word, rest in final if word == "HEALTHCHECK"]
    env_names = [m.group(1) for word, rest in final if word in ("ENV", "ARG")
                 for m in re.finditer(r"(?:^|\s)([A-Za-z_]\w*)(?==|\s|$)", rest)]
    return _Docker(ports=tuple(sorted(set(ports))), odd_ports=tuple(odd), healthcheck=health[-1] if health else None,
                   volumes=tuple(rest for word, rest in final if word == "VOLUME"), env_names=tuple(env_names))


def _norm_py(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pep508_name(requirement: str) -> str | None:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return _norm_py(m.group(1)) if m else None


def _dependencies(files: Mapping[str, str]) -> _Deps:
    names: set[str] = set()
    problems: list[str] = []
    manifests = tuple(m for m in MANIFESTS if m in files)
    if "package.json" in files:
        try:
            deps = json.loads(files["package.json"]).get("dependencies") or {}
            names |= {f"npm:{n}" for n in deps}
        except (ValueError, AttributeError):
            problems.append("package.json 을 읽지 못했다")
    if "requirements.txt" in files:
        for line in files["requirements.txt"].splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith(("-r", "-c", "--requirement", "--constraint")):
                problems.append("requirements.txt 가 다른 파일을 포함한다(-r·-c)")
            elif line and not line.startswith("-") and (name := _pep508_name(line)):
                names.add(f"py:{name}")
    if "pyproject.toml" in files:
        try:
            data = tomllib.loads(files["pyproject.toml"])
            project = data.get("project", {}).get("dependencies", [])
            poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
            names |= {f"py:{n}" for r in project if (n := _pep508_name(r))}
            names |= {f"py:{_norm_py(n)}" for n in poetry if n.lower() != "python"}
        except (tomllib.TOMLDecodeError, AttributeError, TypeError):
            problems.append("pyproject.toml 을 읽지 못했다")
    if "go.mod" in files:
        block = False
        for line in files["go.mod"].splitlines():
            line = line.split("//", 1)[0].strip()
            if line.startswith("require ("):
                block = True
            elif block and line == ")":
                block = False
            elif block and line:
                names.add(f"go:{line.split()[0]}")
            elif line.startswith("require "):
                names.add(f"go:{line.split()[1]}")
    return _Deps(names=frozenset(names), manifests=manifests, problems=tuple(problems))


def _matches(names: Iterable[str], keys: Iterable[str]) -> list[str]:
    keys = tuple(keys)
    return sorted(n for n in names
                  if n in keys or (n.startswith("go:") and any(n.startswith(k + "/") for k in keys)))


def _engine(name: str) -> str:
    return next(engine for key, engine in DRIVERS.items() if name == key or name.startswith(key + "/"))


# --- 항목별 판단 ------------------------------------------------------------------------------------

Decision = tuple[Any, Finding]


def _image(repository: str, workflows: Mapping[str, str]) -> Decision:
    path = "/image"
    if not workflows:
        return None, Finding(path, "-", "CI 워크플로가 없어 이미지 경로를 알 수 없다", False)
    owner, name = repository.split("/", 1)
    texts = []
    for text in workflows.values():
        for expr, value in ((r"github\.repository", repository), (r"github\.repository_owner", owner),
                            (r"github\.event\.repository\.name", name)):
            text = re.sub(r"\$\{\{\s*" + expr + r"\s*\}\}", value, text)
        texts.append(text)
    joined = "\n".join(texts)
    candidates = {re.split(r"[:@]", m.group(0), maxsplit=1)[0].lower().rstrip("/")
                  for m in GHCR.finditer(joined)}
    usable = sorted(c for c in candidates if IMAGE_PATH.fullmatch(c))
    source = ", ".join(sorted(workflows))
    if len(usable) != 1 or len(usable) != len(candidates):
        found = ", ".join(sorted(candidates)) or "없음"
        return None, Finding(path, source, f"ghcr.io 이미지 경로를 하나로 정하지 못했다 ({found})", False)
    arches = sorted({m.group(1) for line in PLATFORM_LINE.findall(joined) for m in LINUX_ARCH.finditer(line)})
    if arches:
        how = f"빌드 플랫폼 {', '.join(arches)}"
    else:
        runners = RUNS_ON.findall(joined)
        if not runners or not all("ubuntu" in r and "arm" not in r for r in runners):
            return None, Finding(path, source, f"{usable[0]} — 빌드 플랫폼을 정하지 못했다(러너 {runners})", False)
        arches, how = ["amd64"], "플랫폼 지정 없음 — ubuntu 러너 기본 amd64"
    return Image(repository=usable[0], platforms=tuple(arches)), Finding(path, source, f"{usable[0]}, {how}", True)


READINESS_ROUTES = ("/readyz", "/readiness", "/ready", "/healthz", "/health")
LIVENESS_ROUTES = ("/livez", "/liveness", "/live", "/healthz", "/health")


def _get_routes(sources: Mapping[str, str]) -> set[str]:
    return {m.group(1) for text in sources.values() for pattern in GET_ROUTES for m in pattern.finditer(text)}


def _runtime(docker: _Docker | None, sources: Mapping[str, str]) -> Decision:
    path = "/runtime"
    if docker is None:
        return None, Finding(path, "-", "루트 Dockerfile 이 없어 포트를 알 수 없다", False)
    if docker.odd_ports or len(docker.ports) != 1:
        exposed = ", ".join([*map(str, docker.ports), *docker.odd_ports]) or "없음"
        return None, Finding(path, "Dockerfile", f"EXPOSE 포트가 하나가 아니다 ({exposed})", False)
    port = docker.ports[0]
    url = LOCAL_URL.search(docker.healthcheck or "")
    if url is None or int(url.group(1) or 80) != port:
        # HEALTHCHECK 가 없으면 소스에 이름이 분명한 헬스 라우트가 있을 때만 쓴다 (/readyz·/livez 등)
        routes = _get_routes(sources)
        readiness = next((r for r in READINESS_ROUTES if r in routes), None)
        liveness = next((r for r in LIVENESS_ROUTES if r in routes), None)
        if readiness or liveness:
            found = " · ".join(f"{k} {v}" for k, v in (("readiness", readiness), ("liveness", liveness)) if v)
            return (Runtime(port=port, health=Health(readiness=readiness, liveness=liveness), resources=BASE_RESOURCES),
                    Finding(path, "Dockerfile, 소스", f"EXPOSE {port}, 소스 라우트 {found}", True))
        note = "HEALTHCHECK·소스 헬스 라우트에 이 포트의 HTTP 경로가 없어 probe 는 비워 둔다"
        return Runtime(port=port, resources=BASE_RESOURCES), Finding(path, "Dockerfile", f"EXPOSE {port} — {note}", True)
    probe = url.group(2) or "/"
    return (Runtime(port=port, health=Health(readiness=probe, liveness=probe), resources=BASE_RESOURCES),
            Finding(path, "Dockerfile", f"EXPOSE {port}, HEALTHCHECK {probe}", True))


def _database(deps: _Deps, sources: Mapping[str, str], gap: str | None) -> Decision:
    path = "/database"
    if not deps.manifests:
        return None, Finding(path, "-", "의존성 파일(package.json·requirements.txt·pyproject.toml·go.mod)이 없다", False)
    source = ", ".join(deps.manifests)
    if deps.problems:
        return None, Finding(path, source, "; ".join(deps.problems), False)
    if stateful := _matches(deps.names, STATEFUL):
        return None, Finding(path, source, f"ORM·다른 저장소 의존성 {_bare(stateful)} — 엔진·배치를 정할 수 없다", False)
    if drivers := _matches(deps.names, DRIVERS):
        engines = sorted({_engine(d) for d in drivers})
        return None, Finding(path, source, f"{_bare(drivers)} → {'/'.join(engines)} 사용. 배치·버전은 레포로 정할 수 없다",
                             False)
    if gap:
        return None, Finding(path, source, f"{gap} — DB 사용 여부를 확인하지 못했다", False)
    if hits := [p for p, text in sources.items() if SQLITE_STDLIB.search(text) or GO_SQL.search(text)]:
        return None, Finding(path, ", ".join(hits), "표준 라이브러리 DB(sqlite·database/sql)를 쓴다", False)
    return Database(), Finding(path, source, "DB 드라이버·ORM 의존성이 없다", True)


def _storage(docker: _Docker | None, deps: _Deps, sources: Mapping[str, str], gap: str | None) -> Decision:
    path = "/storage"
    if docker is not None and docker.volumes:
        return None, Finding(path, "Dockerfile", f"VOLUME {' '.join(docker.volumes)} — 크기·보존을 정할 수 없다", False)
    if found := _matches(deps.names, STORAGE_DEPS):
        return None, Finding(path, ", ".join(deps.manifests), f"업로드·오브젝트 스토리지 의존성 {_bare(found)}", False)
    if gap or not deps.manifests:
        why = gap or "의존성 파일이 없다"
        return None, Finding(path, "-", f"{why} — 파일 저장 여부를 확인하지 못했다", False)
    if writers := [p for p, text in sources.items() if FILE_WRITES.search(text)]:
        return None, Finding(path, ", ".join(writers), "파일을 쓰는 코드가 있다 — 볼륨 필요 여부를 정할 수 없다", False)
    return Storage(), Finding(path, f"소스 {len(sources)}개", "VOLUME·파일 쓰기·스토리지 의존성이 없다", True)


def _env_reads(text: str) -> tuple[set[str], bool]:
    """소스 하나가 읽는 환경변수 이름, 그리고 이름을 알 수 없는 읽기가 있는지."""
    names = {m.group(1) for pattern in ENV_READS for m in pattern.finditer(text)}
    dynamic = False
    for m in ENV_DESTRUCTURE.finditer(text):
        for item in m.group(1).split(","):
            item = item.strip()
            if item.startswith("..."):
                dynamic = True
            elif ident := re.match(r"[A-Za-z_]\w*", item):
                names.add(ident.group(0))
    rest = ENV_DESTRUCTURE.sub("", text)
    return names, dynamic or bool(DYNAMIC_ENV.search(rest))


def _secrets(docker: _Docker | None, deps: _Deps, sources: Mapping[str, str], gap: str | None) -> Decision:
    path = "/secrets"
    if gap:
        return None, Finding(path, "-", f"{gap} — 환경변수를 확인하지 못했다", False)
    if libs := _matches(deps.names, ENV_LIBS):
        return None, Finding(path, ", ".join(deps.manifests),
                             f"설정 라이브러리 {_bare(libs)} 가 환경변수를 읽는다 — 필요한 시크릿을 알 수 없다", False)
    reads = {p: _env_reads(text) for p, text in sources.items()}
    if dynamic := [p for p, (_, unknown) in reads.items() if unknown]:
        return None, Finding(path, ", ".join(dynamic), "환경변수를 이름 없이 읽는다 — 필요한 시크릿을 알 수 없다", False)
    names = {n for found, _ in reads.values() for n in found} | set(docker.env_names if docker else ())
    secret = sorted(n for n in names if is_secret_name(n))
    if undecided := [n for n in secret if not GENERATABLE_SECRET.fullmatch(n)]:
        return None, Finding(path, f"소스 {len(sources)}개",
                             f"비밀로 보이는 환경변수 {', '.join(undecided)} — 어디서 읽을지(source·key)를 정해야 한다", False)
    read = ", ".join(sorted(names)) or "없음"
    if secret:
        refs = tuple(SecretRef(name=n, source="generated") for n in secret)
        why = f"읽는 환경변수({read}) 중 {', '.join(secret)} 는 앱 내부 서명 키라 배포 때 무작위로 만든다(generated)"
        return refs, Finding(path, f"소스 {len(sources)}개", why, True)
    return (), Finding(path, f"소스 {len(sources)}개", f"읽는 환경변수({read}) 중 비밀로 보이는 이름이 없다", True)


def _test_files(tree: Iterable[str]) -> list[str]:
    return sorted(p for p in tree
                  if TEST_NAME.search(PurePosixPath(p).name) or TEST_DIRS.intersection(PurePosixPath(p).parts[:-1]))


def _smoke(tree: Sequence[str], runtime: Runtime | None, sources: Mapping[str, str]) -> Decision:
    """테스트가 없는 앱에만 배포 뒤 확인할 경로를 만든다. 테스트가 있으면 그 테스트가 CI 에서 동작을 확인한다."""
    path = "/smoke"
    if tests := _test_files(tree):
        more = f" 외 {len(tests) - 2}개" if len(tests) > 2 else ""
        return None, Finding(path, ", ".join(tests[:2]) + more, "테스트가 있어 배포 뒤 확인 경로를 만들지 않는다", True)
    readiness = runtime.health.readiness if runtime else None
    routes = sorted(r for r in _get_routes(sources) if PROBE_LIKE.search(r))
    paths = list(dict.fromkeys([*([readiness] if readiness else []), *routes]))[:MAX_SMOKE_PATHS]
    if not paths:
        return None, Finding(path, f"소스 {len(sources)}개", "테스트도, 확인할 GET 경로도 찾지 못했다", False)
    return (Smoke(paths=tuple(paths)),
            Finding(path, f"소스 {len(sources)}개", f"테스트가 없어 배포 뒤 {', '.join(paths)} 를 확인한다", True))


def _requirements(database: Database | None, storage: Storage | None) -> Decision:
    path = "/requirements"
    if database is None or storage is None:
        return None, Finding(path, "-", "DB·저장소를 확정하지 못해 데이터 보존 요구를 정할 수 없다", False)
    # 위 판단은 'DB 없음·저장소 없음'일 때만 값을 낸다
    return Requirements(persistence=False), Finding(path, "database·storage", "DB·볼륨·버킷이 없어 남길 데이터가 없다", True)


def _bare(names: Sequence[str]) -> str:
    return ", ".join(n.split(":", 1)[1] for n in names)


def _gap(tree: Sequence[str], expected: Sequence[str], sources: Mapping[str, str]) -> str | None:
    """'없음' 결론을 막는 이유 — 다 읽었으면 None."""
    if unscanned := _unscanned(tree):
        more = f" 외 {len(unscanned) - 3}개" if len(unscanned) > 3 else ""
        return f"분석하지 않는 파일이 있다({', '.join(unscanned[:3])}{more})"
    if len(sources) < len(expected):
        why = f"최대 {MAX_SOURCE_FILES}개" if len(expected) > MAX_SOURCE_FILES else "큰 파일은 읽지 않는다"
        return f"소스 {len(expected)}개 중 {len(sources)}개만 읽었다({why})"
    return None


def analyze_repository(base: GenerationContext, tree: Iterable[str], files: Mapping[str, str]) -> RepoAnalysis:
    """tree 는 PR head 의 전체 파일 경로, files 는 files_to_read(tree) 중 읽은 것. base 의 레포·대상은 그대로 쓴다."""
    tree = list(tree)
    expected = _source_files(tree)
    sources = {p: files[p] for p in expected if p in files}
    gap = _gap(tree, expected, sources)
    docker = _dockerfile(files["Dockerfile"]) if "Dockerfile" in files else None
    deps = _dependencies(files)
    workflows = {p: text for p, text in files.items() if _is_workflow(p)}

    image, f_image = _image(base.repository, workflows)
    runtime, f_runtime = _runtime(docker, sources)
    database, f_database = _database(deps, sources, gap)
    storage, f_storage = _storage(docker, deps, sources, gap)
    secrets, f_secrets = _secrets(docker, deps, sources, gap)
    requirements, f_requirements = _requirements(database, storage)
    smoke, f_smoke = _smoke(tree, runtime or base.runtime, sources)
    values = {"image": image, "runtime": runtime, "database": database, "storage": storage, "secrets": secrets,
              "requirements": requirements, "smoke": smoke}
    context = base.model_copy(update={k: v for k, v in values.items() if v is not None and getattr(base, k) is None})
    return RepoAnalysis(context=context,
                        findings=(f_image, f_runtime, f_requirements, f_database, f_secrets, f_storage, f_smoke))
