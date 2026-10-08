"""업무 DB 저장소 — reviews·baselines·deploy_events.

ReviewRepository 프로토콜 하나에 Postgres 구현과 메모리 구현(테스트·DB 없는 로컬 실행)을 둔다.
상태 전이는 claim() 의 조건부 UPDATE 로 한다 — Kafka 재전송·중복 웹훅이 와도 한 번만 진행된다.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

ReviewDbStatus = Literal[
    "received", "reviewing", "needs_human", "waiting_ci", "merging", "committed", "blocked", "rejected", "failed",
    "superseded",
]
FINISHED: frozenset[str] = frozenset({"committed", "blocked", "rejected", "failed", "superseded"})
OPEN: tuple[str, ...] = ("received", "reviewing", "needs_human", "waiting_ci")  # 새 커밋이 오면 superseded 로 넘길 상태

JSON_COLUMNS = frozenset({"spec_ref", "decision", "findings", "rounds", "human_decision", "deploy_result", "final_spec"})
UPDATABLE = JSON_COLUMNS | {"status", "verdict", "reasons", "merge_sha", "gitops_commit_sha", "error", "superseded_by"}


def _now() -> datetime:
    return datetime.now(UTC)


def _check_fields(fields: Iterable[str]) -> None:
    unknown = set(fields) - UPDATABLE
    if unknown:
        raise ValueError(f"reviews 에서 바꿀 수 없는 필드: {sorted(unknown)}")


class ReviewRepository(Protocol):
    async def insert_review(self, *, review_id: str, app: str, target_env: str, repo_id: str,
                            spec_ref: dict[str, Any], pr_head_sha: str, requested_by: str,
                            pr_number: int | None = None) -> None: ...

    async def get_review(self, review_id: str) -> dict[str, Any] | None: ...

    async def update_review(self, review_id: str, **fields: Any) -> None:
        """필드를 바꾼다. 단 superseded 인 검토의 status 는 바꾸지 않는다 — 워커가 돌던 중 새 커밋에 밀린 경우."""
        ...

    async def claim(self, review_id: str, *, from_statuses: Sequence[str], to_status: str,
                    pr_head_sha: str | None = None) -> bool:
        """status 가 from_statuses 중 하나일 때만 to_status 로 바꾼다. 바꿨으면 True."""
        ...

    async def latest_by_head_sha(self, sha: str) -> dict[str, Any] | None: ...

    async def find_by_head(self, repository: str, sha: str) -> dict[str, Any] | None:
        """같은 레포(spec_ref.repository)·같은 head SHA 검토. 가장 최근 것."""
        ...

    async def supersede_open(self, *, repository: str, pr_number: int, superseded_by: str) -> list[str]:
        """그 PR 의 끝나지 않은 검토를 superseded 로 넘긴다 (superseded_by 자신은 빼고). 넘긴 review_id 들."""
        ...

    async def waiting_ci_by_head_sha(self, sha: str) -> list[dict[str, Any]]: ...

    async def find_by_merge_sha(self, *, app: str, target_env: str, image_tag: str) -> dict[str, Any] | None:
        """이미지 태그가 merge_sha 와 같은(짧은 SHA 면 앞부분이 같은) 검토. 가장 최근 것."""
        ...

    async def get_baseline(self, app: str, target_env: str) -> dict[str, Any] | None: ...

    async def upsert_baseline(self, *, app: str, target_env: str, spec: dict[str, Any], spec_ref: dict[str, Any],
                              merge_sha: str | None, observed_at: datetime) -> None: ...

    async def add_deploy_event(self, *, review_id: str | None, app: str, target_env: str, kind: str,
                               image_tag: str | None, payload: dict[str, Any]) -> None: ...


def tag_matches(merge_sha: str | None, image_tag: str) -> bool:
    """sample-app CI 는 짧은 SHA(7자) 로 태그를 단다. 7자 미만은 우연히 겹칠 수 있어 받지 않는다."""
    tag = image_tag.lower()
    return bool(merge_sha) and len(tag) >= 7 and merge_sha.lower().startswith(tag)


class PostgresReviewRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _fetchone(self, sql: str, params: Sequence[Any]) -> dict[str, Any] | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def _execute(self, sql: str, params: Sequence[Any]) -> int:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return cur.rowcount

    async def insert_review(self, *, review_id: str, app: str, target_env: str, repo_id: str,
                            spec_ref: dict[str, Any], pr_head_sha: str, requested_by: str,
                            pr_number: int | None = None) -> None:
        await self._execute(
            "INSERT INTO reviews (review_id, app, target_env, repo_id, spec_ref, pr_head_sha, requested_by,"
            " pr_number, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'received')",
            (review_id, app, target_env, repo_id, Jsonb(spec_ref), pr_head_sha, requested_by, pr_number),
        )

    async def get_review(self, review_id: str) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM reviews WHERE review_id = %s", (review_id,))

    async def update_review(self, review_id: str, **fields: Any) -> None:
        if not fields:
            return
        _check_fields(fields)
        columns = sorted(fields)
        assignments = ", ".join(
            "status = CASE WHEN status = 'superseded' THEN status ELSE %s END" if c == "status" else f"{c} = %s"
            for c in columns)
        values = [Jsonb(fields[c]) if c in JSON_COLUMNS and fields[c] is not None else fields[c] for c in columns]
        await self._execute(f"UPDATE reviews SET {assignments}, updated_at = now() WHERE review_id = %s",
                            (*values, review_id))

    async def claim(self, review_id: str, *, from_statuses: Sequence[str], to_status: str,
                    pr_head_sha: str | None = None) -> bool:
        sql = "UPDATE reviews SET status = %s, updated_at = now() WHERE review_id = %s AND status = ANY(%s)"
        params: list[Any] = [to_status, review_id, list(from_statuses)]
        if pr_head_sha is not None:
            sql += " AND pr_head_sha = %s"
            params.append(pr_head_sha)
        return await self._execute(sql, params) == 1

    async def latest_by_head_sha(self, sha: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM reviews WHERE pr_head_sha = %s ORDER BY created_at DESC LIMIT 1", (sha,))

    async def find_by_head(self, repository: str, sha: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM reviews WHERE spec_ref->>'repository' = %s AND pr_head_sha = %s"
            " ORDER BY created_at DESC LIMIT 1", (repository, sha))

    async def supersede_open(self, *, repository: str, pr_number: int, superseded_by: str) -> list[str]:
        rows = await self._fetchall(
            "UPDATE reviews SET status = 'superseded', superseded_by = %s, updated_at = now()"
            " WHERE spec_ref->>'repository' = %s AND pr_number = %s AND review_id <> %s AND status = ANY(%s)"
            " RETURNING review_id", (superseded_by, repository, pr_number, superseded_by, list(OPEN)))
        return [r["review_id"] for r in rows]

    async def waiting_ci_by_head_sha(self, sha: str) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM reviews WHERE pr_head_sha = %s AND status = 'waiting_ci' ORDER BY created_at", (sha,))

    async def find_by_merge_sha(self, *, app: str, target_env: str, image_tag: str) -> dict[str, Any] | None:
        if len(image_tag) < 7:
            return None
        return await self._fetchone(
            "SELECT * FROM reviews WHERE app = %s AND target_env = %s AND merge_sha IS NOT NULL"
            " AND starts_with(lower(merge_sha), lower(%s)) ORDER BY created_at DESC LIMIT 1",
            (app, target_env, image_tag),
        )

    async def get_baseline(self, app: str, target_env: str) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM baselines WHERE app = %s AND target_env = %s", (app, target_env))

    async def upsert_baseline(self, *, app: str, target_env: str, spec: dict[str, Any], spec_ref: dict[str, Any],
                              merge_sha: str | None, observed_at: datetime) -> None:
        # database_has_data 는 관측한 주체가 따로 채운다. 새 배포로 바뀌면 모르는 상태(null)로 되돌린다.
        await self._execute(
            "INSERT INTO baselines (app, target_env, spec, spec_ref, merge_sha, database_has_data, observed_at)"
            " VALUES (%s, %s, %s, %s, %s, NULL, %s)"
            " ON CONFLICT (app, target_env) DO UPDATE SET spec = EXCLUDED.spec, spec_ref = EXCLUDED.spec_ref,"
            " merge_sha = EXCLUDED.merge_sha, database_has_data = NULL, observed_at = EXCLUDED.observed_at",
            (app, target_env, Jsonb(spec), Jsonb(spec_ref), merge_sha, observed_at),
        )

    async def add_deploy_event(self, *, review_id: str | None, app: str, target_env: str, kind: str,
                               image_tag: str | None, payload: dict[str, Any]) -> None:
        await self._execute(
            "INSERT INTO deploy_events (review_id, app, target_env, kind, image_tag, payload)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (review_id, app, target_env, kind, image_tag, Jsonb(payload)),
        )


class InMemoryReviewRepository:
    """테스트·DB 없는 로컬 실행용. Postgres 구현과 같은 규칙으로 동작한다."""

    def __init__(self) -> None:
        self.reviews: dict[str, dict[str, Any]] = {}
        self.baselines: dict[tuple[str, str], dict[str, Any]] = {}
        self.deploy_events: list[dict[str, Any]] = []

    async def insert_review(self, *, review_id: str, app: str, target_env: str, repo_id: str,
                            spec_ref: dict[str, Any], pr_head_sha: str, requested_by: str,
                            pr_number: int | None = None) -> None:
        if review_id in self.reviews:
            raise ValueError(f"review_id 중복: {review_id}")
        now = _now()
        self.reviews[review_id] = {
            "review_id": review_id, "app": app, "target_env": target_env, "repo_id": repo_id,
            "spec_ref": copy.deepcopy(spec_ref), "pr_head_sha": pr_head_sha, "merge_sha": None,
            "status": "received", "verdict": None, "reasons": [], "decision": None, "findings": None,
            "rounds": None, "human_decision": None, "deploy_result": None, "gitops_commit_sha": None,
            "final_spec": None, "error": None, "superseded_by": None, "requested_by": requested_by,
            "pr_number": pr_number, "created_at": now, "updated_at": now,
        }

    async def get_review(self, review_id: str) -> dict[str, Any] | None:
        row = self.reviews.get(review_id)
        return copy.deepcopy(row) if row else None

    async def update_review(self, review_id: str, **fields: Any) -> None:
        _check_fields(fields)
        row = self.reviews.get(review_id)
        if row is None:
            return
        fields = copy.deepcopy(fields)
        if row["status"] == "superseded":
            fields.pop("status", None)
        row.update(fields, updated_at=_now())

    async def claim(self, review_id: str, *, from_statuses: Sequence[str], to_status: str,
                    pr_head_sha: str | None = None) -> bool:
        row = self.reviews.get(review_id)
        if row is None or row["status"] not in from_statuses:
            return False
        if pr_head_sha is not None and row["pr_head_sha"] != pr_head_sha:
            return False
        row.update(status=to_status, updated_at=_now())
        return True

    def _latest(self, rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
        ordered = sorted(rows, key=lambda r: r["created_at"])
        return copy.deepcopy(ordered[-1]) if ordered else None

    async def latest_by_head_sha(self, sha: str) -> dict[str, Any] | None:
        return self._latest(r for r in self.reviews.values() if r["pr_head_sha"] == sha)

    async def find_by_head(self, repository: str, sha: str) -> dict[str, Any] | None:
        return self._latest(r for r in self.reviews.values()
                            if r["spec_ref"].get("repository") == repository and r["pr_head_sha"] == sha)

    async def supersede_open(self, *, repository: str, pr_number: int, superseded_by: str) -> list[str]:
        done = []
        for r in sorted(self.reviews.values(), key=lambda r: r["created_at"]):
            if (r["spec_ref"].get("repository") == repository and r["pr_number"] == pr_number
                    and r["review_id"] != superseded_by and r["status"] in OPEN):
                r.update(status="superseded", superseded_by=superseded_by, updated_at=_now())
                done.append(r["review_id"])
        return done

    async def waiting_ci_by_head_sha(self, sha: str) -> list[dict[str, Any]]:
        rows = [r for r in self.reviews.values() if r["pr_head_sha"] == sha and r["status"] == "waiting_ci"]
        return copy.deepcopy(sorted(rows, key=lambda r: r["created_at"]))

    async def find_by_merge_sha(self, *, app: str, target_env: str, image_tag: str) -> dict[str, Any] | None:
        return self._latest(r for r in self.reviews.values()
                            if r["app"] == app and r["target_env"] == target_env
                            and tag_matches(r["merge_sha"], image_tag))

    async def get_baseline(self, app: str, target_env: str) -> dict[str, Any] | None:
        row = self.baselines.get((app, target_env))
        return copy.deepcopy(row) if row else None

    async def upsert_baseline(self, *, app: str, target_env: str, spec: dict[str, Any], spec_ref: dict[str, Any],
                              merge_sha: str | None, observed_at: datetime) -> None:
        self.baselines[(app, target_env)] = {
            "app": app, "target_env": target_env, "spec": copy.deepcopy(spec), "spec_ref": copy.deepcopy(spec_ref),
            "merge_sha": merge_sha, "database_has_data": None, "observed_at": observed_at,
        }

    async def add_deploy_event(self, *, review_id: str | None, app: str, target_env: str, kind: str,
                               image_tag: str | None, payload: dict[str, Any]) -> None:
        self.deploy_events.append({
            "id": len(self.deploy_events) + 1, "review_id": review_id, "app": app, "target_env": target_env,
            "kind": kind, "image_tag": image_tag, "payload": copy.deepcopy(payload), "received_at": _now(),
        })


def make_pool(conninfo: str, *, min_size: int = 1, max_size: int = 5) -> AsyncConnectionPool:
    """autocommit 풀. LangGraph AsyncPostgresSaver 도 같은 설정(autocommit·dict_row)을 요구한다."""
    return AsyncConnectionPool(conninfo, min_size=min_size, max_size=max_size, open=False,
                               kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
