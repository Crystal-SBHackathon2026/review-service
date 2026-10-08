"""환경변수 읽기. DB 계정은 k8s Secret review-db-credentials 에서 DB_USERNAME·DB_PASSWORD 로 들어온다."""

from __future__ import annotations

import os

from psycopg.conninfo import make_conninfo


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"환경변수 {name} 가 없다")
    return value


def db_conninfo() -> str:
    # RDS 파라미터 그룹이 rds.force_ssl=1 이므로 클러스터에서는 DB_SSLMODE=require 를 준다.
    return make_conninfo(
        host=env("DB_HOST", "localhost"),
        port=env("DB_PORT", "5432"),
        dbname=env("DB_NAME", "oneaction_review"),
        user=env("DB_USERNAME"),
        password=env("DB_PASSWORD"),
        sslmode=env("DB_SSLMODE", "prefer"),
        connect_timeout="10",
    )


def kafka_bootstrap() -> str:
    return env("KAFKA_BOOTSTRAP", "localhost:9092")
