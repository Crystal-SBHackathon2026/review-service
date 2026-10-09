"""마이그레이션 SQL 판정과 RUN-008 — 선언한 스키마 변경이 실제 SQL 과 맞는지."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from review_ai.graph import check_edited_ops
from review_ai.messages import ReviewRequested
from review_ai.migrations import classify, classify_statement, new_migration_files
from review_ai.patching import PatchError
from review_ai.spec.deploy_spec import DeploySpec, user_fields
from review_ai.static_check import run_static_check
from tests.conftest import load_sample_dict


@pytest.mark.parametrize(("sql", "change"), [
    ("CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY)", "expand"),
    ("CREATE UNIQUE INDEX i ON t (x)", "expand"),
    ("ALTER TABLE todos ADD COLUMN due_at TIMESTAMPTZ", "expand"),
    ("ALTER TABLE todos ADD COLUMN done BOOLEAN NOT NULL DEFAULT false", "expand"),
    ("ALTER TABLE todos ALTER COLUMN title DROP NOT NULL", "expand"),
    ("UPDATE todos SET done = false WHERE done IS NULL", "expand"),
    ("DROP INDEX i", "expand"),
    ("ALTER TABLE todos DROP CONSTRAINT todos_title_key", "expand"),
    ("ALTER TABLE todos DROP COLUMN legacy", "contract"),
    ("DROP TABLE IF EXISTS legacy", "contract"),
    ("ALTER TABLE todos ADD COLUMN done BOOLEAN NOT NULL", "breaking"),
    ("ALTER TABLE todos RENAME COLUMN title TO name", "breaking"),
    ("ALTER TABLE todos RENAME TO tasks", "breaking"),
    ("ALTER TABLE todos ALTER COLUMN title TYPE VARCHAR(10)", "breaking"),
    ("ALTER TABLE todos ALTER COLUMN title SET NOT NULL", "breaking"),
    ("ALTER TABLE todos ADD CONSTRAINT u UNIQUE (title)", "breaking"),
    ("DELETE FROM todos", "breaking"),
    ("VACUUM FULL todos", "breaking"),  # 모르는 문은 사람이 본다
    ("ALTER TABLE t ADD COLUMN a INT, DROP COLUMN b", "breaking"),  # 한 문 안에 expand + contract
])
def test_statement_classification(sql: str, change: str) -> None:
    assert classify_statement(sql) == change


@pytest.mark.parametrize("sql", ["BEGIN", "COMMIT", "SET lock_timeout = '5s'", "COMMENT ON TABLE t IS 'x'"])
def test_transaction_and_session_statements_are_ignored(sql: str) -> None:
    assert classify_statement(sql) is None


def test_file_mixing_expand_and_contract_is_breaking() -> None:
    result = classify({"migrations/0003.sql": "-- 새 컬럼\nALTER TABLE t ADD COLUMN a INT;\n/* 옛 컬럼 */ DROP TABLE old;"})
    assert result.change == "breaking"
    assert result.evidence == ("migrations/0003.sql: expand ALTER TABLE t ADD COLUMN a INT",
                               "migrations/0003.sql: contract DROP TABLE old")


def test_empty_or_comment_only_is_none() -> None:
    assert classify({"migrations/0004.sql": "-- 나중에\n"}).change == "none"


def test_new_files_are_those_after_the_deployed_commit() -> None:
    deployed = ["migrations/0001_init.sql", "src/app.js"]
    current = [*deployed, "migrations/0002_add.sql", "db/migrations/0003.sql", "migrations/README.md"]
    assert new_migration_files(current, deployed) == ["db/migrations/0003.sql", "migrations/0002_add.sql"]
    assert new_migration_files(current, None) == ["db/migrations/0003.sql", "migrations/0001_init.sql",
                                                  "migrations/0002_add.sql"]


def spec_with(declared: str | None, observed: str | None, engine: str = "postgres") -> dict[str, Any]:
    raw = load_sample_dict("01-pass-sample-app-aws.yaml")
    raw["database"] = {"engine": engine, "version": "16", "placement": "managed"}
    raw["secrets"] = [{"name": "DATABASE_URL", "source": "aws-secrets-manager", "key": "a/b"}]
    if declared:
        raw["database"]["migration"] = {"command": ["node", "src/migrate.js"], "change": declared}
    if observed:
        raw["observed"] = {"schema_change": observed, "migrations": ["migrations/0002.sql"]}
    return raw


def run_008(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in run_static_check(DeploySpec.model_validate(raw)) if f["rule_id"] == "RUN-008"]


@pytest.mark.parametrize(("declared", "observed", "hit"), [
    ("expand", "expand", False), ("expand", "contract", True), ("contract", "expand", True),
    ("expand", "breaking", True), ("breaking", "contract", False), ("none", "expand", True),
    ("expand", None, False), ("expand", "none", False),
])
def test_run_008_compares_declared_with_sql(declared: str, observed: str | None, hit: bool) -> None:
    findings = run_008(spec_with(declared, observed))
    assert bool(findings) is hit
    if hit:
        assert findings[0]["autofix"] == "allowed" and findings[0]["location"]["spec_path"] == "/database/migration/change"


def test_new_sql_without_migration_command_needs_a_human() -> None:
    [finding] = run_008(spec_with(None, "expand"))
    assert finding["location"]["spec_path"] == "/database/migration"


def test_observed_is_pipeline_only() -> None:
    raw = spec_with("expand", "contract")
    with pytest.raises(PatchError, match="observed"):
        check_edited_ops(raw, [{"op": "replace", "path": "/observed/schema_change", "value": "expand"}])
    assert "observed" not in user_fields(raw) and "baseline" not in user_fields(raw)
    with pytest.raises(ValidationError, match="observed"):
        ReviewRequested.model_validate({
            "review_id": "r", "repo_id": "x/y", "app": "sample-app", "target_env": "aws",
            "spec_ref": {"repository": "x/y", "commit": "a" * 40}, "spec_sha256": "0" * 64,
            "deploy_spec": raw, "requested_by": "t", "requested_at": "2026-10-09T00:00:00Z"})
