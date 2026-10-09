"""무엇을 고칠지 — 레포 분석 결과와 대상 환경 능력표로 코드 패치 항목을 정한다 (LLM 없음).

| 항목 | 조건 | 결과 |
|---|---|---|
| DB_SQLITE_TO_POSTGRES | Node 앱이 SQLite 드라이버를 쓰는데 대상 환경이 SQLite 볼륨을 못 만든다 | pg 드라이버·DATABASE_URL·마이그레이션 파일·migrate 스크립트 |
| METRICS_ENDPOINT | Express 앱에 /metrics 가 없다 | prom-client 로 GET /metrics |

자동 패치는 Node(package.json) 앱만 한다. 다른 언어는 항목을 만들지 않고 이유만 남긴다 — 그러면 레포 분석의
미해결(DATABASE_UNVERIFIED)이 그대로 남아 지금처럼 커밋하지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from review_ai.catalog import TargetCaps
from review_ai.intake.analyze import DRIVERS, _dependencies, _get_routes, _source_files
from review_ai.spec.deploy_spec import Database, Migration, SecretRef

ItemCode = Literal["DB_SQLITE_TO_POSTGRES", "METRICS_ENDPOINT"]
MIGRATE_SCRIPT = "src/migrate.js"
MIGRATIONS_DIR = "migrations"
MIGRATE_COMMAND = ("node", MIGRATE_SCRIPT)
DATABASE_ENV = "DATABASE_URL"
SQLITE_DRIVERS = frozenset(name for name, engine in DRIVERS.items() if engine == "sqlite")  # 생태계:이름
CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?(\w+)", re.IGNORECASE)
# 대상 환경별 DATABASE_URL 시크릿 위치 — DB 를 만드는 쪽(Crossplane·Terraform)이 같은 이름으로 채운다
SECRET_SOURCE = {"aws": "aws-secrets-manager", "gcp": "gcp-secret-manager", "local": "k8s-secret"}
PLACEMENT_ORDER = ("managed", "in-cluster")


@dataclass(frozen=True)
class PlanItem:
    code: ItemCode
    reason: str
    instructions: tuple[str, ...]
    add_dependencies: frozenset[str]     # 새로 넣어도 되는 npm 패키지
    remove_dependencies: frozenset[str]  # 빼야 하는 npm 패키지


@dataclass(frozen=True)
class TransformPlan:
    items: tuple[PlanItem, ...]
    skipped: tuple[str, ...]             # 고쳐야 하지만 자동 패치를 하지 않는 이유
    database: Database | None = None     # 변환 뒤 명세 값
    secrets: tuple[SecretRef, ...] = ()
    tables: tuple[str, ...] = ()         # 마이그레이션이 만들어야 하는 앱 테이블 (원래 소스의 CREATE TABLE)

    @property
    def codes(self) -> set[str]:
        return {item.code for item in self.items}


def _sqlite_supported(caps: TargetCaps) -> bool:
    return caps.supports_db("volume", "sqlite", None) and bool(caps.volume_access_modes)


def _postgres_target(caps: TargetCaps) -> Database | None:
    for placement in PLACEMENT_ORDER:
        versions = caps.database.get(placement, {}).get("postgres")
        if versions is not None:
            return Database(engine="postgres", placement=placement,  # type: ignore[arg-type]
                            version=versions[0] if versions else None, env_var=DATABASE_ENV,
                            migration=Migration(command=MIGRATE_COMMAND, change="expand"))
    return None


SESSION_TABLES = frozenset({"session", "sessions"})  # 세션 저장소 테이블은 connect-pg-simple 의 session 으로 바뀐다


def _tables(sources: Mapping[str, str]) -> tuple[str, ...]:
    """마이그레이션이 만들어야 하는 앱 테이블 — 세션 테이블은 이름이 바뀌므로 따로 확인한다."""
    found = {m.group(1).lower() for text in sources.values() for m in CREATE_TABLE.finditer(text)}
    return tuple(sorted(found - SESSION_TABLES))


def _db_item(app: str, caps: TargetCaps, sqlite: Sequence[str], database: Database) -> tuple[PlanItem, SecretRef]:
    names = ", ".join(n.split(":", 1)[1] for n in sqlite)
    where = f"{database.placement} postgres{f' {database.version}' if database.version else ''}"
    key = "database-url" if caps.env == "local" else f"{app}/database-url"
    secret = SecretRef(name=DATABASE_ENV, source=SECRET_SOURCE[caps.env], key=key)  # type: ignore[arg-type]
    instructions = (
        f"SQLite 드라이버({names})를 pg(node-postgres)로 바꾼다. 접속은 process.env.{DATABASE_ENV} 하나로만 한다 — "
        "호스트·계정·비밀번호를 코드에 쓰지 않는다. DB 파일 경로용 환경변수(DATA_DIR 등)는 더 이상 읽지 않는다",
        "쿼리는 pg 의 $1·$2 자리표시자와 async/await 로 바꾼다. 동작(경로·응답 모양·상태 코드)은 그대로 둔다",
        "SQLite 전용 문법을 Postgres 로 바꾼다: INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY, "
        "CURRENT_TIMESTAMP 텍스트 → TIMESTAMPTZ DEFAULT now(), pragma 제거",
        f"테이블 생성(CREATE TABLE)은 앱 시작 코드에서 빼서 {MIGRATIONS_DIR}/0001_init.sql 로 옮긴다. "
        "IF NOT EXISTS 를 붙여 여러 번 실행해도 되게 한다",
        f"{MIGRATE_SCRIPT} 를 만든다: {MIGRATIONS_DIR}/*.sql 을 이름 순서로, 아직 적용 안 된 것만 트랜잭션 안에서 실행하고 "
        "schema_migrations(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now()) 에 기록한다. "
        f"process.env.{DATABASE_ENV} 로 접속하고 끝나면 연결을 닫는다. 배포 때 Kubernetes Job 이 `node {MIGRATE_SCRIPT}` 로 실행한다",
        'package.json scripts 에 "migrate": "node src/migrate.js" 를 더한다',
        "SQLite 에 세션을 저장하던 코드는 connect-pg-simple 로 바꾸고, 세션 테이블도 0001_init.sql 에 만든다"
        "(connect-pg-simple 기본 스키마: session(sid varchar PRIMARY KEY, sess json NOT NULL, expire timestamp(6) NOT NULL)"
        " + expire 인덱스). SQLite 전용 세션 저장소 파일은 지운다",
        "readiness 경로는 DB 에 SELECT 1 을 보내 확인하는 동작을 유지한다",
    )
    item = PlanItem("DB_SQLITE_TO_POSTGRES",
                    f"{caps.env} 는 SQLite 볼륨을 만들 수 없다 — {where} 로 바꾼다", instructions,
                    add_dependencies=frozenset({"pg", "connect-pg-simple"}),
                    remove_dependencies=frozenset(n.split(":", 1)[1] for n in sqlite))
    return item, secret


METRICS_ITEM = PlanItem(
    "METRICS_ENDPOINT", "Express 앱에 /metrics 가 없다 — Prometheus 가 수집할 지표를 노출한다",
    ("prom-client 로 GET /metrics 를 추가한다: collectDefaultMetrics() + 요청 수 Counter"
     "(http_requests_total, 라벨 method·route·status) + 처리 시간 Histogram(http_request_duration_seconds)",
     "route 라벨에는 실제 URL 이 아니라 req.route?.path(없으면 'unmatched')를 써서 라벨 수가 늘지 않게 한다",
     "/metrics 는 로그인 없이 열리고 정적 파일·세션 미들웨어보다 앞에 둔다"),
    add_dependencies=frozenset({"prom-client"}), remove_dependencies=frozenset())


def plan_transform(app: str, caps: TargetCaps, tree: Sequence[str], files: Mapping[str, str]) -> TransformPlan:
    """files 는 레포 분석이 읽은 파일(files_to_read). tree 는 PR head 의 전체 경로."""
    deps = _dependencies(files)
    sources = {p: files[p] for p in _source_files(tree) if p in files}
    items: list[PlanItem] = []
    skipped: list[str] = []
    database: Database | None = None
    secrets: tuple[SecretRef, ...] = ()
    node = "package.json" in deps.manifests and not deps.problems
    sqlite = sorted(deps.names & SQLITE_DRIVERS)  # npm·py·go 모두 — 자동 패치는 아래에서 npm 만
    tables: tuple[str, ...] = ()
    if sqlite and not _sqlite_supported(caps):
        target = _postgres_target(caps)
        if target is None:
            skipped.append(f"{caps.env} 에 SQLite 도 Postgres 도 없다 — DB 를 사람이 정한다")
        elif not node:
            skipped.append("SQLite → Postgres 자동 패치는 Node(package.json) 앱만 한다")
        else:
            item, secret = _db_item(app, caps, sqlite, target)
            items.append(item)
            database, secrets = target, (secret,)
            tables = _tables(sources)
    if node and "npm:express" in deps.names and "/metrics" not in _get_routes(sources):
        items.append(METRICS_ITEM)
    return TransformPlan(tuple(items), tuple(skipped), database, secrets, tables)
