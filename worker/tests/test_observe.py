"""검토 전 관측 — 배포된 커밋 이후 새 마이그레이션 SQL 을 판정해 deploy_spec.observed 로 넣고, RUN-008 이 선언과 대조한다."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from review_ai.messages import build_review_requested
from review_common.github import GitHubError
from review_worker.handler import ReviewHandler
from review_worker.observe import MAX_MIGRATION_FILES, observe_migrations
from tests.conftest import HEAD, Harness, load_sample

DEPLOYED = "d" * 40
REPO = "Crystal-SBHackathon2026/sample-app"
INIT = "CREATE TABLE IF NOT EXISTS todos (id SERIAL PRIMARY KEY, title TEXT NOT NULL);"


class Files:
    def __init__(self, trees: dict[str, dict[str, str]], error: GitHubError | None = None) -> None:
        self.trees, self.error, self.reads = trees, error, []

    async def list_files(self, repository: str, ref: str) -> list[str]:
        if self.error:
            raise self.error
        return list(self.trees[ref])

    async def get_file(self, repository: str, path: str, ref: str) -> str:
        self.reads.append(path)
        return self.trees[ref][path]


def trees(new_sql: str | None) -> dict[str, dict[str, str]]:
    deployed = {"deploy.yaml": "", "src/server.js": "", "migrations/0001_init.sql": INIT}
    head = dict(deployed)
    if new_sql is not None:
        head["migrations/0002_change.sql"] = new_sql
    return {DEPLOYED: deployed, HEAD: head}


async def test_only_migrations_after_the_deployed_commit_are_read() -> None:
    files = Files(trees("ALTER TABLE todos DROP COLUMN legacy;"))
    observed = await observe_migrations(files, REPO, HEAD, {"merge_sha": DEPLOYED})
    assert observed == {"schema_change": "contract", "migrations": ["migrations/0002_change.sql"],
                        "evidence": ["migrations/0002_change.sql: contract ALTER TABLE todos DROP COLUMN legacy"]}
    assert files.reads == ["migrations/0002_change.sql"]


async def test_no_new_migration_means_no_observation() -> None:
    assert await observe_migrations(Files(trees(None)), REPO, HEAD, {"merge_sha": DEPLOYED}) is None


async def test_first_deploy_reads_every_migration() -> None:
    observed = await observe_migrations(Files(trees(None)), REPO, HEAD, None)
    assert observed and observed["schema_change"] == "expand"
    assert observed["migrations"] == ["migrations/0001_init.sql"]


async def test_unknown_deployed_commit_or_github_error_skips_observation() -> None:
    assert await observe_migrations(Files(trees("DROP TABLE x;")), REPO, HEAD, {"merge_sha": None}) is None
    broken = Files(trees("DROP TABLE x;"), error=GitHubError("boom", 502))
    assert await observe_migrations(broken, REPO, HEAD, {"merge_sha": DEPLOYED}) is None


async def test_too_many_new_files_is_treated_as_breaking() -> None:
    many = trees(None)
    many[HEAD].update({f"migrations/{i:04d}.sql": "CREATE INDEX i ON t (x);" for i in range(2, MAX_MIGRATION_FILES + 3)})
    observed = await observe_migrations(Files(many), REPO, HEAD, {"merge_sha": DEPLOYED})
    assert observed and observed["schema_change"] == "breaking" and "중" in observed["evidence"][-1]


def postgres_sample(change: str) -> dict[str, Any]:
    spec = copy.deepcopy(load_sample("01-pass-sample-app-aws.yaml"))
    spec["database"] = {"engine": "postgres", "version": "16", "placement": "managed",
                        "migration": {"command": ["node", "src/migrate.js"], "change": change}}
    spec["secrets"] = [{"name": "DATABASE_URL", "source": "aws-secrets-manager", "key": "sample/database-url"}]
    return spec


async def test_worker_fills_observed_and_run_008_fixes_the_declared_change() -> None:
    h = Harness()
    await h.repo.upsert_baseline(app="sample-app", target_env="aws", spec=postgres_sample("expand"),
                                 merge_sha=DEPLOYED, spec_ref={"repository": REPO, "commit": DEPLOYED,
                                                               "path": "deploy.yaml"},
                                 observed_at=datetime.now(UTC))
    h.handler = ReviewHandler(h.repo, h.graph, files=Files(trees("ALTER TABLE todos DROP COLUMN legacy;")))
    await h.request(postgres_sample("expand"), review_id="rv_obs")

    row = await h.repo.get_review("rv_obs")
    rounds = row["rounds"] or []
    assert any(f["rule_id"] == "RUN-008" for r in rounds for f in r.get("findings", [])), rounds
    final = row["final_spec"]
    assert final["database"]["migration"]["change"] == "contract"
    assert "observed" not in final and "baseline" not in final  # 다음 배포의 baseline.spec 이 된다


def test_message_refuses_pipeline_fields() -> None:
    spec = {**postgres_sample("expand"), "observed": {"schema_change": "expand"}}
    msg = build_review_requested(spec, review_id="r", spec_ref={"repository": REPO, "commit": HEAD,
                                                                 "path": "deploy.yaml"},
                                 requested_by="t", requested_at=datetime.now(UTC))
    assert "observed" not in msg.deploy_spec
