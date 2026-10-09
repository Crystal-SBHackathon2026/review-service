"""규칙 ID → 검사 함수. 각 함수는 CheckContext 를 받아 Hit 목록을 돌려준다 (순수 함수)."""

from __future__ import annotations

from collections.abc import Callable

from review_ai.static_check.context import CheckContext, Hit
from review_ai.static_check.rules import database, network, runtime, secret, storage

Check = Callable[[CheckContext], list[Hit]]

CHECKS: dict[str, Check] = {
    "DB-001": database.engine_change_with_data,
    "DB-002": database.unsupported_engine,
    "DB-003": database.sqlite_replicated,
    "DB-004": database.persistence_without_storage,
    "DB-005": database.sqlite_without_persistent_volume,
    "DB-006": database.managed_db_public,
    "DB-007": database.managed_db_without_backup,
    "DB-008": database.version_downgrade,
    "SEC-001": secret.plaintext_secret,
    "SEC-002": secret.unsupported_secret_source,
    "SEC-003": secret.duplicate_secret,
    "SEC-004": secret.env_shadowed_by_secret,
    "SEC-005": secret.missing_db_secret,
    "NET-001": network.public_without_tls,
    "NET-002": network.internal_open_to_all,
    "STO-001": storage.unsupported_access_mode,
    "STO-002": storage.rwo_volume_replicated,
    "STO-003": storage.public_bucket,
    "STO-004": storage.unencrypted_bucket,
    "STO-005": storage.volume_shrink,
    "STO-006": storage.persistent_volume_removed,
    "RUN-001": runtime.missing_readiness,
    "RUN-002": runtime.missing_liveness,
    "RUN-004": runtime.arch_mismatch,
    "RUN-005": runtime.missing_resource_limits,
}
