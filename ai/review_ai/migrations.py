"""마이그레이션 SQL 판정 — 이번 배포에 새로 들어온 SQL 이 스키마를 어떻게 바꾸는가 (설계 '앱 분석/변환' ⑤).

워커가 배포된 커밋(baseline merge_sha) 이후 새로 생긴 migrations/*.sql 을 읽어 deploy_spec.observed 에 넣는다.
선언(database.migration.change)과 다르면 RUN-008 이 고친다 → 실행 시점(PreSync·PostSync)과 전략(RUN-006·007)이 따라온다.

| 판정 | 문 |
|---|---|
| expand | CREATE TABLE·INDEX·VIEW·SEQUENCE·TYPE·EXTENSION, ADD COLUMN(NULL 허용 또는 DEFAULT), DROP NOT NULL·SET DEFAULT, DROP INDEX·CONSTRAINT, INSERT·UPDATE(백필) |
| contract | DROP TABLE·COLUMN·VIEW·TYPE |
| breaking | RENAME, ALTER COLUMN TYPE, SET NOT NULL, NOT NULL·DEFAULT 없는 ADD COLUMN, ADD CONSTRAINT, DELETE·TRUNCATE, 모르는 문 |

한 배포에 expand 와 contract 가 섞이면 breaking 이다 — expand 는 새 버전 전에, contract 는 뒤에 돌아야 해서 한 Job 으로 못 돈다.
모르는 문은 breaking 으로 본다(사람이 확인) — 판정을 놓쳐 canary 로 나가는 것보다 낫다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from review_ai.secrets_pattern import redact
from review_ai.spec.deploy_spec import SchemaChange

MIGRATION_SQL = re.compile(r"(?:^|/)migrations/(?:[^/]+/)?[^/]+\.sql$")
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_IGNORED = re.compile(r"^(BEGIN|COMMIT|START TRANSACTION|SET\b|COMMENT ON|GRANT|REVOKE|ANALYZE|SELECT\b)", re.IGNORECASE)

# (판정, 패턴) — 위에서부터 먼저 맞는 것. ALTER TABLE 안의 여러 동작은 쉼표로 나눠 하나씩 본다
_RULES: tuple[tuple[SchemaChange, re.Pattern[str]], ...] = tuple((change, re.compile(pattern, re.IGNORECASE)) for change, pattern in (
    ("breaking", r"\bRENAME\b"),
    ("breaking", r"\bALTER\s+(?:COLUMN\s+)?\S+\s+(?:SET\s+DATA\s+)?TYPE\b"),
    ("breaking", r"\bSET\s+NOT\s+NULL\b"),
    ("breaking", r"^ADD\s+(?:CONSTRAINT|PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK)\b"),
    ("expand", r"^ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?\S+\s.*\bDEFAULT\b"),
    ("breaking", r"^ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?\S+\s.*\bNOT\s+NULL\b"),
    ("expand", r"^ADD\s+(?:COLUMN\s+)?"),
    ("contract", r"^DROP\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW|TYPE)\b"),
    ("expand", r"^DROP\s+(?:INDEX|SEQUENCE)\b"),
    ("contract", r"^DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?(?!CONSTRAINT\b|DEFAULT\b|NOT\b)\S+"),
    ("expand", r"^DROP\s+(?:CONSTRAINT|DEFAULT|NOT\s+NULL)\b"),
    ("expand", r"^ALTER\s+(?:COLUMN\s+)?\S+\s+(?:DROP\s+NOT\s+NULL|SET\s+DEFAULT|DROP\s+DEFAULT)\b"),
    ("expand", r"^CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|VIEW|MATERIALIZED\s+VIEW|SEQUENCE|TYPE|EXTENSION|OR\s+REPLACE\s+VIEW|SCHEMA)\b"),
    ("expand", r"^(?:INSERT|UPDATE)\b"),
    ("breaking", r"^(?:DELETE|TRUNCATE)\b"),
))
_ALTER_TABLE = re.compile(r"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?\S+\s+(.*)$", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Classified:
    change: SchemaChange
    evidence: tuple[str, ...]  # "<파일>: <판정> <문 앞부분>" — 판정에 쓴 문만


def _statements(sql: str) -> list[str]:
    text = _COMMENTS.sub(" ", sql)
    return [" ".join(s.split()) for s in text.split(";") if s.strip()]


def _actions(statement: str) -> list[str]:
    """ALTER TABLE t A, B → [A, B]. 다른 문은 그대로 하나. 괄호 안 쉼표는 나누지 않는다."""
    m = _ALTER_TABLE.match(statement)
    if not m:
        return [statement]
    parts, depth, current = [], 0, ""
    for ch in m.group(1):
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    return [p for p in [*parts, current.strip()] if p]


def classify_statement(statement: str) -> SchemaChange | None:
    """문 하나. 무시하는 문(BEGIN·SET·COMMENT …)은 None."""
    if _IGNORED.match(statement):
        return None
    found: list[SchemaChange] = []
    for action in _actions(statement):
        change = next((c for c, pattern in _RULES if pattern.search(action)), "breaking")
        found.append(change)
    return _combine(found)


def _combine(changes: Iterable[SchemaChange]) -> SchemaChange:
    kinds = set(changes) - {"none"}
    if "breaking" in kinds or {"expand", "contract"} <= kinds:
        return "breaking"
    return next(iter(kinds), "none")  # type: ignore[return-value]


def classify(files: dict[str, str]) -> Classified:
    """새로 들어온 마이그레이션 파일들 → 이번 배포의 스키마 변경 하나."""
    changes: list[SchemaChange] = []
    evidence: list[str] = []
    for path, sql in sorted(files.items()):
        for statement in _statements(sql):
            change = classify_statement(statement)
            if change is None:
                continue
            changes.append(change)
            evidence.append(redact(f"{path}: {change} {statement[:80]}"))  # 근거는 프롬프트·업무 DB 로 간다
    return Classified(_combine(changes), tuple(evidence[:20]))


def new_migration_files(current: Iterable[str], deployed: Iterable[str] | None) -> list[str]:
    """배포된 커밋 이후 생긴 마이그레이션 파일. 배포된 적 없으면 전부 새 파일이다."""
    seen = set(deployed or ())
    return sorted(p for p in current if MIGRATION_SQL.search(p) and p not in seen)
