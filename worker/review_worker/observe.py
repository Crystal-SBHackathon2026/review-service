"""검토 전에 관측 사실을 채운다 — 배포된 커밋 이후 새로 생긴 마이그레이션 SQL 의 판정 (deploy_spec.observed).

baseline 처럼 메시지에는 싣지 않고 워커가 처리 시점에 채운다. 판정은 review_ai.migrations, 규칙은 RUN-008.
- 배포된 적 없으면 레포의 마이그레이션 파일이 전부 새 파일이다
- baseline 은 있는데 배포 커밋을 모르면(merge_sha·spec_ref 없음) 무엇이 새것인지 몰라 채우지 않는다
- GitHub 오류는 검토를 막지 않는다 — 로그를 남기고 채우지 않는다(관측 없음 = RUN-008 이 걸리지 않음)
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from review_ai.migrations import classify, new_migration_files
from review_common.github import GitHubError

log = logging.getLogger(__name__)

MAX_MIGRATION_FILES = 20


class RepoFiles(Protocol):
    async def list_files(self, repository: str, ref: str) -> list[str]: ...

    async def get_file(self, repository: str, path: str, ref: str) -> str: ...


def deployed_commit(baseline_row: dict[str, Any]) -> str | None:
    return baseline_row.get("merge_sha") or (baseline_row.get("spec_ref") or {}).get("commit")


async def observe_migrations(github: RepoFiles, repository: str, commit: str,
                             baseline_row: dict[str, Any] | None) -> dict[str, Any] | None:
    deployed: list[str] | None = None
    try:
        if baseline_row is not None:
            base = deployed_commit(baseline_row)
            if base is None:
                return None
            deployed = await github.list_files(repository, base)
        new = new_migration_files(await github.list_files(repository, commit), deployed)
        if not new:
            return None
        texts = {p: await github.get_file(repository, p, commit) for p in new[:MAX_MIGRATION_FILES]}
    except GitHubError as exc:
        log.warning("%s@%s: 마이그레이션 관측 실패 — 채우지 않는다: %s", repository, commit[:7], exc)
        return None
    result = classify(texts)
    evidence = list(result.evidence)
    if len(new) > MAX_MIGRATION_FILES:  # 다 못 읽은 파일이 있으면 가장 보수적으로
        evidence.append(f"새 마이그레이션 {len(new)}개 중 {MAX_MIGRATION_FILES}개만 읽었다 — breaking 으로 본다")
        return {"schema_change": "breaking", "migrations": new, "evidence": evidence}
    return {"schema_change": result.change, "migrations": new, "evidence": evidence}
