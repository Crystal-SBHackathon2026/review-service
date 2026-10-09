"""새 앱 코드 패치 — SQLite 앱(sample-todo)을 AWS 에 올리려면 Postgres 로 바꾼 코드·잠금 파일·명세를 한 커밋에."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from review_ai.judge.fake_llm import ScriptedLLM
from review_ai.spec.deploy_spec import AppSpec
from review_api.lockfile import LockfileUnavailable
from tests.test_api import HEAD, pr_event, send_pr
from tests.test_intake import IntakeEnv

FIXTURES = Path(__file__).resolve().parents[2] / "ai" / "tests" / "fixtures" / "transform"
OLD_LOCK = '{"name": "sample-todo", "lockfileVersion": 3, "packages": {}}'
NEW_LOCK = '{"name": "sample-todo", "lockfileVersion": 3, "packages": {"node_modules/pg": {}}}'


def read_tree(name: str) -> dict[str, str]:
    root = FIXTURES / name
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


def good_answer(_request: Any = None) -> dict[str, Any]:
    patched = read_tree("sample-todo-patched")
    changes = [{"path": p, "action": "write", "content": t} for p, t in patched.items()]
    changes.append({"path": "src/sqlite-session-store.js", "action": "delete", "content": ""})
    return {"changes": changes, "notes": [{"path": "src/server.js", "why": "pg 로 바꿨다"}]}


class Lockfile:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error = error

    async def __call__(self, package_json: str, lock: str) -> str:
        self.calls.append((package_json, lock))
        if self.error:
            raise self.error
        return NEW_LOCK


def todo_env(llm: Any = None, lockfile: Lockfile | None = None, **extra_files: str) -> IntakeEnv:
    env = IntakeEnv(transform_llm=llm, lockfile=lockfile or Lockfile())
    env.github.tree = {**read_tree("sample-todo"), "package-lock.json": OLD_LOCK, "README.md": "# todo\n",
                       **extra_files}
    return env


def test_sqlite_app_gets_postgres_code_lockfile_and_spec_in_one_commit() -> None:
    llm, lockfile = ScriptedLLM(good_answer), Lockfile()
    env = todo_env(llm, lockfile)
    send_pr(env, pr_event("opened"))

    row = env.only_intake()
    commit = row["result_commit_sha"]
    assert (row["status"], row["reason"], env.github.branch) == ("generated", "TRANSFORMED", commit)
    files = env.github.files[commit]
    assert files["src/sqlite-session-store.js"] is None
    assert files["package-lock.json"] == NEW_LOCK and "better-sqlite3" not in (files["package.json"] or "")
    assert {"src/server.js", "src/migrate.js", "migrations/0001_init.sql", "deploy.yaml"} <= set(files)
    [(package_json, lock)] = lockfile.calls
    assert lock == OLD_LOCK and json.loads(package_json)["dependencies"]["pg"]

    spec = AppSpec.model_validate(yaml.safe_load(files["deploy.yaml"]))
    assert (spec.database.engine, spec.database.placement, spec.database.migration.command) == (
        "postgres", "managed", ("node", "src/migrate.js"))
    assert {s.name for s in spec.secrets} == {"DATABASE_URL", "SESSION_SECRET"}
    message = env.github.last_message
    assert message.startswith("feat: ") and "코드 패치" in message and "- src/server.js: pg 로 바꿨다" in message
    assert "- package-lock.json:" in message
    assert llm.calls == 1


def test_without_transform_llm_sqlite_app_is_rejected_with_the_reason() -> None:
    env = todo_env(None)
    send_pr(env, pr_event("opened"))

    row = env.only_intake()
    assert (row["status"], row["reason"], row["result_commit_sha"]) == ("rejected", "TRANSFORM_UNAVAILABLE", None)
    assert "SQLite 볼륨" in row["message"] and env.github.parents == {}


def test_gate_rejection_is_reported_not_committed() -> None:
    def leaks(_request: Any) -> dict[str, Any]:
        answer = good_answer()
        server = next(c for c in answer["changes"] if c["path"] == "src/server.js")
        server["content"] = server["content"].replace("connectionString: DATABASE_URL",
                                                      "connectionString: 'postgres://u:pw@db/app'")
        return answer

    env = todo_env(ScriptedLLM(leaks))
    send_pr(env, pr_event("opened"))
    row = env.only_intake()
    assert (row["status"], row["reason"]) == ("rejected", "TRANSFORM_REJECTED")
    assert "SECRET_LITERAL" in {d["code"] for d in row["details"]} and env.github.parents == {}


def test_lockfile_failure_stops_the_commit() -> None:
    env = todo_env(ScriptedLLM(good_answer), Lockfile(LockfileUnavailable("npm 이 없다")))
    send_pr(env, pr_event("opened"))

    row = env.only_intake()
    assert (row["status"], row["reason"]) == ("rejected", "LOCKFILE_UNAVAILABLE")
    assert row["details"][0]["message"] == "npm 이 없다" and env.github.parents == {}


def test_other_package_managers_lockfiles_are_not_regenerated() -> None:
    lockfile = Lockfile()
    env = todo_env(ScriptedLLM(good_answer), lockfile, **{"yarn.lock": "# yarn\n"})
    send_pr(env, pr_event("opened"))

    assert env.only_intake()["reason"] == "LOCKFILE_UNAVAILABLE" and lockfile.calls == []


def test_llm_is_not_called_when_other_values_are_still_unknown() -> None:
    llm = ScriptedLLM(good_answer)
    env = todo_env(llm)
    del env.github.tree["Dockerfile"]  # 포트를 모른다 — 코드를 고쳐도 명세를 만들 수 없다
    send_pr(env, pr_event("opened"))

    row = env.only_intake()
    assert (row["status"], row["reason"], llm.calls) == ("rejected", "UNVERIFIED", 0)
    assert env.github.statuses[-1]["sha"] == HEAD
