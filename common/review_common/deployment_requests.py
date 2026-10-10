"""다중 환경 요청 저장소. 외부 작업을 포함한 조정은 PR 단위 session lock으로 직렬화한다."""
from __future__ import annotations

import asyncio
import copy
import hashlib
from contextlib import asynccontextmanager

from psycopg.types.json import Jsonb


class RequestBusy(RuntimeError):
    pass

TERMINAL = frozenset({"committed", "superseded", "blocked"})
FIELDS = frozenset({"state", "pending_sha", "merge_sha", "gitops_commit_sha", "error", "expected_snapshot"})


def request_id(repository, pr_number, sha, path):
    return "dr_" + hashlib.sha256(f"{repository}:{pr_number}:{sha}:{path}".encode()).hexdigest()[:32]


def lock_key(repository, pr_number):
    return int.from_bytes(hashlib.sha256(f"multitarget:{repository}:{pr_number}".encode()).digest()[:8],
                          "big", signed=True)


class PostgresRequests:
    async def insert_request(self, row):
        await self._execute(
            "INSERT INTO deployment_requests(request_id,repository,pr_number,head_sha,manifest_path,targets,"
            "requested_by,fix_count) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (row["request_id"], row["repository"], row["pr_number"], row["head_sha"], row["manifest_path"],
             Jsonb(row["targets"]), row["requested_by"], row.get("fix_count", 0)))

    async def get_request(self, rid):
        return await self._fetchone("SELECT * FROM deployment_requests WHERE request_id=%s", (rid,))

    async def update_request(self, rid, **fields):
        if not fields or not set(fields) <= FIELDS:
            raise ValueError("invalid deployment request update")
        columns = sorted(fields)
        await self._execute("UPDATE deployment_requests SET " + ",".join(f"{c}=%s" for c in columns) +
                            ",updated_at=now() WHERE request_id=%s", (*[Jsonb(fields[c]) if c == "expected_snapshot" else fields[c] for c in columns], rid))

    async def pending_requests(self):
        return await self._fetchall("SELECT * FROM deployment_requests WHERE NOT state=ANY(%s) "
                                    "ORDER BY updated_at LIMIT 50", (list(TERMINAL),))

    async def requests_for_pr(self, repository, number):
        return await self._fetchall("SELECT * FROM deployment_requests WHERE repository=%s AND pr_number=%s",
                                    (repository, number))

    async def request_children(self, rid):
        return await self._fetchall("SELECT * FROM reviews WHERE deployment_request_id=%s ORDER BY target_env", (rid,))

    async def insert_request_retry(self, retry_id, rid, env):
        await self._execute("INSERT INTO deployment_request_retries(retry_id,request_id,target_env) VALUES(%s,%s,%s) "
                            "ON CONFLICT DO NOTHING", (retry_id, rid, env))
        return await self._fetchone("SELECT * FROM deployment_request_retries WHERE retry_id=%s", (retry_id,))

    async def pending_request_retries(self):
        return await self._fetchall("SELECT * FROM deployment_request_retries WHERE state='pending' ORDER BY created_at LIMIT 50", ())

    async def finish_request_retry(self, retry_id, *, sha=None, error=None):
        await self._execute("UPDATE deployment_request_retries SET state=%s,gitops_commit_sha=%s,error=%s WHERE retry_id=%s",
                            ("blocked" if error else "committed", sha, error, retry_id))

    @asynccontextmanager
    async def request_lock(self, repository, number):
        # Separate connection: each durable state update commits before an external mutation.
        async with self._pool.connection() as conn:
            key = lock_key(repository, number)
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,))
            if not (await cur.fetchone())["acquired"]:
                raise RequestBusy("another operation is coordinating this PR")
            await conn.commit()
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
                await conn.commit()


class MemoryRequests:
    async def insert_request(self, row):
        self.requests.setdefault(row["request_id"], {**copy.deepcopy(row), "state": "reviewing",
                                                   "pending_sha": None, "merge_sha": None,
                                                   "gitops_commit_sha": None, "expected_snapshot": None, "error": None})

    async def get_request(self, rid):
        return copy.deepcopy(self.requests.get(rid))

    async def update_request(self, rid, **fields):
        if not fields or not set(fields) <= FIELDS:
            raise ValueError("invalid deployment request update")
        self.requests[rid].update(copy.deepcopy(fields))

    async def pending_requests(self):
        return [copy.deepcopy(r) for r in self.requests.values() if r["state"] not in TERMINAL][:50]

    async def requests_for_pr(self, repository, number):
        return [copy.deepcopy(r) for r in self.requests.values()
                if r["repository"] == repository and r["pr_number"] == number]

    async def request_children(self, rid):
        return [copy.deepcopy(r) for r in self.reviews.values() if r.get("deployment_request_id") == rid]

    async def insert_request_retry(self, retry_id, rid, env):
        self.request_retries.setdefault(retry_id, dict(retry_id=retry_id, request_id=rid, target_env=env,
                                                       state="pending", gitops_commit_sha=None, error=None))
        return copy.deepcopy(self.request_retries[retry_id])

    async def pending_request_retries(self):
        return [copy.deepcopy(r) for r in self.request_retries.values() if r["state"] == "pending"][:50]

    async def finish_request_retry(self, retry_id, *, sha=None, error=None):
        self.request_retries[retry_id].update(state="blocked" if error else "committed", gitops_commit_sha=sha, error=error)

    @asynccontextmanager
    async def request_lock(self, repository, number):
        lock = self.request_locks.setdefault((repository, number), asyncio.Lock())
        async with lock:
            yield


async def activate_multitarget(pool):
    """Only after API and workers have all been upgraded with the feature disabled.

    0013 installs the new single-target index alongside the old one. Removing the old,
    broader constraint is deferred until explicit activation, so merely deploying code
    cannot break the old worker's ON CONFLICT statement during a rolling upgrade.
    """
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (0x0EAC7104,))
        await conn.execute("DROP INDEX IF EXISTS reviews_repo_head_open_uniq")
