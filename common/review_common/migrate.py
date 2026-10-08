"""업무 DB 마이그레이션 — migrations/NNNN_*.sql 을 이름 순서대로 한 번씩 적용한다.

API·워커가 동시에 떠도 advisory lock 으로 한 곳만 적용한다.
    python -m review_common.migrate
"""

from __future__ import annotations

import asyncio
import logging
from importlib import resources

import psycopg

from review_common.settings import db_conninfo

log = logging.getLogger(__name__)
LOCK_KEY = 0x0EAC7104  # 임의의 고정값. 같은 DB 를 쓰는 다른 앱과 겹치지 않으면 된다


def migration_files() -> list[tuple[str, str]]:
    folder = resources.files("review_common") / "migrations"
    files = sorted(p for p in folder.iterdir() if p.name.endswith(".sql"))
    return [(p.name, p.read_text(encoding="utf-8")) for p in files]


async def migrate(conninfo: str) -> list[str]:
    applied: list[str] = []
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cur = await conn.execute("SELECT name FROM schema_migrations")
            done = {row[0] for row in await cur.fetchall()}
            for name, sql in migration_files():
                if name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
                log.info("migration applied: %s", name)
                applied.append(name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return applied


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    applied = asyncio.run(migrate(db_conninfo()))
    print(f"applied: {applied or 'none'}")


if __name__ == "__main__":
    main()
