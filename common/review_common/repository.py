"""업무 DB 저장소 — reviews·baselines·deploy_events·spec_intakes·review_cases.

ReviewRepository 프로토콜 하나에 Postgres 구현과 메모리 구현(테스트·DB 없는 로컬 실행)을 둔다.
상태 전이는 claim() 의 조건부 UPDATE 로 한다 — Kafka 재전송·중복 웹훅이 와도 한 번만 진행된다.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from review_common.deployment_requests import MemoryRequests, PostgresRequests
from review_common.deployment_repository import LAST_DEPLOY_FAILED, MemoryDeployments, PostgresDeployments

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

ReviewDbStatus = Literal[
    "received", "reviewing", "needs_human", "waiting_ci", "merging", "committed", "blocked", "rejected", "failed",
    "superseded",
]
FINISHED: frozenset[str] = frozenset({"committed", "blocked", "rejected", "failed", "superseded"})
OPEN: tuple[str, ...] = ("received", "reviewing", "needs_human", "waiting_ci")  # 새 커밋이 오면 superseded 로 넘길 상태
# 0008 부분 unique 인덱스에서 빠지는 상태 — 같은 레포·head SHA 라도 새 검토를 넣을 수 있다
HEAD_REUSABLE: tuple[str, ...] = ("failed", "superseded")
# review sweep 이 회수하는 상태 — 워커가 처리 중이어야 하는 상태. needs_human·waiting_ci 는 사람·CI 를 기다린다
RECOVERABLE: tuple[str, ...] = ("received", "reviewing", "merging")

JSON_COLUMNS = frozenset({"spec_ref", "decision", "findings", "rounds", "human_decision", "deploy_result", "final_spec", "case_advice"})
STAGE_TIMES = frozenset({"judged_at", "human_decided_at", "merged_at", "gitops_committed_at"})  # 0009, 커밋 타임라인
UPDATABLE = JSON_COLUMNS | STAGE_TIMES | {"status", "verdict", "reasons", "merge_sha", "gitops_commit_sha", "error",
                                         "superseded_by"}

INTAKE_JSON_COLUMNS = frozenset({"errors", "details"})
INTAKE_FIELDS = ("intake_id", "repository", "head_repository", "pr_number", "head_sha", "head_ref", "path", "kind",
                 "errors", "requested_by")
INTAKE_FINISH = frozenset({"status", "reason", "message", "details", "result_commit_sha"})
GENERATED_KINDS = ("missing", "empty")  # intake 가 명세를 새로 만든 경우. 형식 오류 복구(repaired)는 원문 값을 지킨다

LIST_FIELDS = ("review_id", "app", "target_env", "spec_ref", "status", "verdict", "reasons", "requested_by", "created_at",
               "updated_at", "pr_number")  # list_reviews — 승인 화면 목록. findings·decision 같은 큰 JSON 은 빼고

CASE_FIELDS = ("case_id", "review_id", "app", "target_env", "rule_ids", "outcome", "summary", "ops")


def _now() -> datetime:
    return datetime.now(UTC)


def _check_fields(fields: Iterable[str]) -> None:
    unknown = set(fields) - UPDATABLE
    if unknown:
        raise ValueError(f"reviews 에서 바꿀 수 없는 필드: {sorted(unknown)}")


def _check_intake(intake: dict[str, Any], finish: dict[str, Any]) -> None:
    if set(intake) != set(INTAKE_FIELDS):
        raise ValueError(f"spec_intakes 필드가 맞지 않다: {sorted(set(intake) ^ set(INTAKE_FIELDS))}")
    unknown = set(finish) - INTAKE_FINISH
    if unknown or finish.get("status") == "processing":
        raise ValueError(f"spec_intakes 를 끝낼 수 없는 필드·상태: {sorted(unknown) or finish['status']}")


def _check_case(case: dict[str, Any]) -> None:
    if set(case) != set(CASE_FIELDS):
        raise ValueError(f"review_cases 필드가 맞지 않다: {sorted(set(case) ^ set(CASE_FIELDS))}")


def baseline_for(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """업무 DB baselines 행 → deploy_spec['baseline'] (review_ai Baseline 모양)."""
    if row is None:
        return None
    ref = row["merge_sha"] or (row.get("spec_ref") or {}).get("commit") or f"{row['app']}/{row['target_env']}"
    observed = row.get("observed_at")
    return {
        "spec_ref": ref,
        "spec": row["spec"],
        "facts": {"database_has_data": row.get("database_has_data"),
                  "observed_at": observed.isoformat() if observed else None},
    }


# committed 검토 → baselines 행 모양. 배포 확인 전이라 데이터 유무를 모른다 — None 은 데이터 있음으로 취급된다.
_COMMITTED_AS_BASELINE = ("app, target_env, final_spec AS spec, spec_ref, merge_sha,"
                          " NULL::boolean AS database_has_data, NULL::timestamptz AS observed_at")


class ReviewRepository(Protocol):
    # Dedicated deployment evidence and analysis; implementations share the same business DB.
    async def find_deployment_review(self, app: str, env: str, revision: str | None) -> dict[str, Any] | None: ...
    async def record_deployment(self, event: dict[str, Any]) -> bool: ...
    async def get_deployment(self, event_id: str) -> dict[str, Any] | None: ...
    async def list_deployments(self, review_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...
    async def has_failed_deployment(self, review_id: str) -> bool:
        """대상 환경에서 실패가 한 번이라도 관측됐는지 — 해결 근거(성공 배포) 자격용. 회복해도 True 다."""
        ...

    async def is_deployment_failing(self, review_id: str) -> bool:
        """대상 환경의 마지막 배포 알림이 실패(degraded·sync_failed)인지 — baseline·진행 화면 판단용.
        실패 뒤 Healthy 가 오면 False 로 돌아온다."""
        ...

    async def mirror_deployment_event(self, *, event_id: str, review_id: str, app: str, target_env: str,
                                      kind: str, image_tag: str | None, payload: dict[str, Any]) -> None: ...
    async def lease_analysis_publications(self) -> list[dict[str, Any]]: ...
    async def claim_analysis(self, event_id: str) -> dict[str, Any] | None: ...
    async def finish_analysis(self, event_id: str, token: str, *, status: str, result: dict[str, Any] | None = None,
                              error_code: str | None = None, case: dict[str, Any] | None = None) -> bool: ...
    async def save_failure_case(self, case: dict[str, Any]) -> None: ...
    async def get_failure_case(self, case_id: str) -> dict[str, Any] | None: ...
    async def find_failure_cases(self, *, app: str, repository: str, target_env: str, limit: int = 20) -> list[dict[str, Any]]: ...
    async def confirm_resolution(self, case_id: str, resolution: dict[str, Any]) -> bool: ...

    async def insert_review(self, *, review_id: str, app: str, target_env: str, repo_id: str,
                            spec_ref: dict[str, Any], pr_head_sha: str, requested_by: str,
                            pr_number: int | None = None, deployment_request_id: str | None = None) -> str:
        """received 로 넣고 review_id 를 돌려준다. 같은 repo_id·pr_head_sha 의 검토(failed·superseded 빼고)가
        이미 있으면 넣지 않고 그 review_id 를 돌려준다 — 같은 웹훅이 동시에 와도 검토는 하나."""
        ...

    async def get_review(self, review_id: str) -> dict[str, Any] | None: ...

    async def update_review(self, review_id: str, **fields: Any) -> None:
        """필드를 바꾼다. 단 superseded 인 검토의 status 는 바꾸지 않는다 — 워커가 돌던 중 새 커밋에 밀린 경우."""
        ...

    async def claim(self, review_id: str, *, from_statuses: Sequence[str], to_status: str,
                    pr_head_sha: str | None = None) -> bool:
        """status 가 from_statuses 중 하나일 때만 to_status 로 바꾼다. 바꿨으면 True."""
        ...

    async def latest_by_head_sha(self, sha: str) -> dict[str, Any] | None: ...

    async def list_reviews(self, statuses: Sequence[str], limit: int) -> list[dict[str, Any]]:
        """status 가 statuses 중 하나인 검토 limit 개, 최신(created_at) 순. statuses 가 비면 전부. 필드는 LIST_FIELDS."""
        ...

    async def find_by_head(self, repository: str, sha: str) -> dict[str, Any] | None:
        """같은 레포(spec_ref.repository)·같은 head SHA 검토. 가장 최근 것.

        failed 는 뺀다 — 발행 실패 등으로 끝난 검토가 있어도 같은 SHA 가 다시 오면 새로 검토한다."""
        ...

    async def supersede_open(self, *, repository: str, pr_number: int, superseded_by: str | None,
                             error: str | None = None) -> list[str]:
        """그 PR 의 끝나지 않은 검토를 superseded 로 넘긴다 (superseded_by 자신은 빼고). 넘긴 review_id 들.

        superseded_by=None — 새 커밋에 검토할 명세가 없다 (intake 로 갔다) 또는 PR 이 병합 없이 닫혔다(error="PR closed")."""
        ...

    async def waiting_ci_by_head_sha(self, sha: str) -> list[dict[str, Any]]: ...

    async def status_counts(self, since: timedelta) -> list[dict[str, Any]]:
        """updated_at 이 since 안인 검토의 (app, target_env, status) 별 개수 — /metrics 게이지 review_reviews."""
        ...

    async def needs_human_oldest(self) -> list[dict[str, Any]]:
        """(app, target_env) 별 가장 오래 기다린 needs_human 검토의 대기 초 (updated_at 기준)."""
        ...

    async def claim_stale_review(self, older_than: timedelta, statuses: Sequence[str]) -> dict[str, Any] | None:
        """status 가 statuses 중 하나이고 updated_at 이 older_than 보다 오래된 검토 하나를 가져간다.

        recover_count 를 1 올리고 updated_at 을 새로 찍은 행을 돌려준다. 없으면 None.
        API 파드 여럿이 동시에 sweep 해도 한 행은 한 곳만 가져간다 (조건부 UPDATE·SKIP LOCKED).
        """
        ...

    async def find_by_merge_sha(self, *, app: str, target_env: str | None, image_tag: str) -> dict[str, Any] | None:
        """이미지 태그가 merge_sha 와 같은(짧은 SHA 면 앞부분이 같은) 검토. 가장 최근 것.

        target_env=None 이면 대상 환경을 보지 않는다 — 검토 대상이 아닌 환경(aws 검토의 local 배포)의 알림을 잇는다."""
        ...

    async def find_by_merge_sha_exact(self, merge_sha: str) -> dict[str, Any] | None:
        """merge_sha 가 정확히 같은 검토. 가장 최근 것 — baseline 이 어느 검토의 배포인지 찾는다."""
        ...

    async def latest_merged_review(self, app: str, target_env: str) -> dict[str, Any] | None:
        """그 앱·대상 환경에서 가장 최근에 병합한(merge_sha 가 있는) 검토 — 실패 알림을 옛 검토에 잇지 않게."""
        ...

    async def get_baseline(self, app: str, target_env: str) -> dict[str, Any] | None: ...

    async def baseline_or_last_committed(self, app: str, target_env: str) -> dict[str, Any] | None:
        """검토가 비교할 이전 배포 — baselines 행, 없으면 같은 app·target_env 로 gitops 에 마지막으로 커밋한
        검토(committed)의 final_spec 을 baselines 행 모양으로(database_has_data=None → 데이터 있음으로 취급).

        baselines 는 Argo Notifications 웹훅으로만 쓰인다. local(자체 Argo CD, pull 모델)·gcp 는 웹훅이 닿지 않거나
        구독이 없어 비어 있고, 그러면 STO-006(볼륨 제거)·STO-005·DB-001 이 걸리지 않는다. 대체 규칙은
        latest_baseline_for_repository 와 같다.
        """
        ...

    async def upsert_baseline(self, *, app: str, target_env: str, spec: dict[str, Any], spec_ref: dict[str, Any],
                              merge_sha: str | None, observed_at: datetime) -> None: ...

    async def add_deploy_event(self, *, review_id: str | None, app: str, target_env: str, kind: str,
                               image_tag: str | None, payload: dict[str, Any]) -> None: ...

    async def has_deploy_event(self, *, review_id: str, target_env: str, kind: str, image_tag: str | None) -> bool:
        """같은 (검토, 환경, healthy|degraded, 이미지 태그) 배포 알림을 이미 기록했는지. Argo CD 가 같은 알림을 다시 보낸다.

        환경이 다르면 다른 알림이다 — 같은 병합을 aws·local 이 각각 배포한다."""
        ...

    async def last_deploy_event(self, *, review_id: str, target_env: str) -> dict[str, Any] | None:
        """그 검토·환경의 마지막 배포 알림 (kind·image_tag·received_at). 없으면 None."""
        ...

    async def deploy_events_for(self, review_id: str) -> list[dict[str, Any]]:
        """그 검토의 배포 알림(payload 포함), 받은 순서(received_at)대로 — 진행 화면의 환경별 배포 상태.
        payload 는 Rollout 자동 중단 판단에만 쓰고 화면 응답에 내보내지 않는다."""
        ...

    async def find_superseding_parent(self, review_id: str) -> dict[str, Any] | None:
        """superseded_by 가 review_id 인 검토 (넘겨준 쪽). 가장 최근 것 — 진행 화면이 검토 이력을 거꾸로 따라간다."""
        ...

    async def latest_baseline_for_repository(self, repository: str) -> dict[str, Any] | None:
        """그 레포(spec_ref.repository)의 가장 최근 baseline — 명세가 없을 때 앱·대상 환경을 거기서 찾는다.

        배포 확인(Argo 웹훅) 전이라 baselines 가 비었으면 마지막으로 gitops 에 커밋한 검토의 final_spec 을
        baselines 행 모양으로 돌려준다. 이미 배포된 앱을 새 앱으로 보고 ingress 등을 지운 사고(sample-app #11) 방지.
        """
        ...

    # --- spec_intakes ---

    async def insert_intake(self, **intake: Any) -> bool:
        """processing 으로 넣는다. 같은 레포·head SHA 행이 이미 있으면 넣지 않고 False."""
        ...

    async def get_intake(self, intake_id: str) -> dict[str, Any] | None: ...

    async def find_intake_by_head(self, repository: str, sha: str) -> dict[str, Any] | None: ...

    async def latest_intake_by_head_sha(self, sha: str) -> dict[str, Any] | None: ...

    async def find_intake_by_result_commit(self, repository: str, sha: str) -> dict[str, Any] | None:
        """intake 가 만든 커밋이 sha 인 행 — 루프 방지·검토 연결용."""
        ...

    async def find_intake_by_reviews(self, review_ids: Sequence[str]) -> dict[str, Any] | None:
        """review_id 가 review_ids 중 하나인 intake. 가장 최근 것 — 진행 화면의 명세 생성 단계."""
        ...

    async def finish_intake(self, intake_id: str, **fields: Any) -> bool:
        """processing 일 때만 끝낸다. 끝냈으면 True — 두 곳이 같은 행을 처리해도 결과는 하나만 남는다."""
        ...

    async def link_intake(self, intake_id: str, *, result_commit_sha: str | None = None,
                          review_id: str | None = None, baseline_used: bool | None = None,
                          unverified_paths: Sequence[str] | None = None) -> str | None:
        """만든 커밋·그 커밋의 검토·baseline 으로 만들었는지·확인하지 못한 경로를 잇는다. None 인 값은 그대로 둔다.
        행에 남은 result_commit_sha 를 돌려준다.

        result_commit_sha 는 이미 있으면 덮지 않는다 — 두 곳이 같은 행을 처리해도 먼저 이은 커밋 하나로 브랜치를 옮긴다.
        baseline_used·unverified_paths 도 처음 기록한 값을 지킨다 — 다시 처리하는 행은 처음 만든 커밋을 그대로 쓰기 때문이다."""
        ...

    async def touch_intake(self, intake_id: str) -> None:
        """processing 인 행의 updated_at 을 새로 찍는다 — 처리 중이라는 heartbeat. sweep 이 가져가지 않게."""
        ...

    async def unverified_generation_for_pr(self, repository: str, pr_number: int) -> dict[str, Any] | None:
        """그 PR 에 intake 가 baseline 없이, 확인하지 못한 값을 후보값으로 채워 만든 명세(missing·empty) 커밋이 있으면 그 행.

        baseline_used 가 NULL 인 행(0006 전·기록 실패)도 baseline 없이 만든 것으로 본다 — 모르면 사람에게.
        unverified_paths 가 빈 배열이면(전부 확인된 생성 명세) 해당하지 않는다. NULL(0012 전 행)은 해당한다."""
        ...

    async def claim_stale_intakes(self, older_than: timedelta) -> list[dict[str, Any]]:
        """older_than 보다 오래 processing 인 행(처리 중 파드가 죽은 것)을 가져가며 updated_at 을 새로 찍고
        attempts 를 1 올린다 (insert_intake 가 1 로 넣는다). 돌려주는 행의 attempts 는 올린 뒤 값이다.

        API 가 여러 개여도 한 행은 한 곳만 가져간다. 상한을 넘었는지는 호출하는 쪽(intake)이 판단한다."""
        ...

    # --- review_cases ---

    async def insert_case(self, **case: Any) -> bool:
        """사례를 넣는다 (review_ai.cases.build_case 결과). 같은 case_id 가 이미 있으면 넣지 않고 False."""
        ...

    async def find_cases(self, rule_id: str, *, app: str, repository: str, target_env: str,
                         outcomes: Sequence[str], limit: int) -> list[dict[str, Any]]:
        """rule_id 가 걸린 같은 앱·레포 사례 중 outcomes 인 것 limit 개 — 같은 대상 환경을 먼저, 그 안에서 최근 것부터.

        레포는 사례의 검토(reviews.spec_ref.repository)로 본다."""
        ...


def tag_matches(merge_sha: str | None, image_tag: str) -> bool:
    """sample-app CI 는 짧은 SHA(7자) 로 태그를 단다. 7자 미만은 우연히 겹칠 수 있어 받지 않는다."""
    tag = image_tag.lower()
    return bool(merge_sha) and len(tag) >= 7 and merge_sha.lower().startswith(tag)


class PostgresReviewRepository(PostgresRequests, PostgresDeployments):
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
                            pr_number: int | None = None, deployment_request_id: str | None = None) -> str:
        if deployment_request_id is not None:
            await self._execute(
                "INSERT INTO reviews(review_id,app,target_env,repo_id,spec_ref,pr_head_sha,requested_by,pr_number,"
                "deployment_request_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (review_id, app, target_env, repo_id, Jsonb(spec_ref), pr_head_sha, requested_by, pr_number,
                 deployment_request_id))
            row = await self._fetchone("SELECT review_id FROM reviews WHERE deployment_request_id=%s AND target_env=%s",
                                       (deployment_request_id, target_env))
            return row["review_id"]
        for _ in range(3):  # 겹친 행이 그사이 failed·superseded 가 되면 다시 넣는다
            row = await self._fetchone(
                "INSERT INTO reviews (review_id, app, target_env, repo_id, spec_ref, pr_head_sha, requested_by,"
                " pr_number, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'received')"
                " ON CONFLICT (repo_id, pr_head_sha) WHERE status NOT IN ('failed', 'superseded') AND deployment_request_id IS NULL DO NOTHING"
                " RETURNING review_id",
                (review_id, app, target_env, repo_id, Jsonb(spec_ref), pr_head_sha, requested_by, pr_number))
            if row is None:
                row = await self._fetchone(
                    "SELECT review_id FROM reviews WHERE repo_id = %s AND pr_head_sha = %s AND deployment_request_id IS NULL AND NOT status = ANY(%s)",
                    (repo_id, pr_head_sha, list(HEAD_REUSABLE)))
            if row is not None:
                return row["review_id"]
        raise RuntimeError(f"검토를 넣지 못했다: {repo_id}@{pr_head_sha}")

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

    async def list_reviews(self, statuses: Sequence[str], limit: int) -> list[dict[str, Any]]:
        where = " WHERE status = ANY(%s)" if statuses else ""
        params: list[Any] = [list(statuses)] if statuses else []
        return await self._fetchall(
            f"SELECT {', '.join(LIST_FIELDS)} FROM reviews{where} ORDER BY created_at DESC, review_id DESC LIMIT %s",
            (*params, limit))

    async def find_by_head(self, repository: str, sha: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM reviews WHERE spec_ref->>'repository' = %s AND pr_head_sha = %s AND status <> 'failed'"
            " ORDER BY created_at DESC LIMIT 1", (repository, sha))

    async def supersede_open(self, *, repository: str, pr_number: int, superseded_by: str | None,
                             error: str | None = None) -> list[str]:
        rows = await self._fetchall(
            "UPDATE reviews SET status = 'superseded', superseded_by = %s, error = COALESCE(%s, error),"
            " updated_at = now()"
            " WHERE spec_ref->>'repository' = %s AND pr_number = %s AND review_id IS DISTINCT FROM %s::text"
            " AND status = ANY(%s)"
            " RETURNING review_id", (superseded_by, error, repository, pr_number, superseded_by, list(OPEN)))
        return [r["review_id"] for r in rows]

    async def waiting_ci_by_head_sha(self, sha: str) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM reviews WHERE pr_head_sha = %s AND status = 'waiting_ci' ORDER BY created_at", (sha,))

    async def status_counts(self, since: timedelta) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT app, target_env, status, count(*) AS count FROM reviews WHERE updated_at > now() - %s"
            " GROUP BY 1, 2, 3", (since,))

    async def needs_human_oldest(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT app, target_env, extract(epoch FROM now() - min(updated_at))::float8 AS seconds FROM reviews"
            " WHERE status = 'needs_human' GROUP BY 1, 2", ())

    async def claim_stale_review(self, older_than: timedelta, statuses: Sequence[str]) -> dict[str, Any] | None:
        return await self._fetchone(
            "UPDATE reviews SET recover_count = recover_count + 1, updated_at = now() WHERE review_id = ("
            " SELECT review_id FROM reviews WHERE status = ANY(%s) AND updated_at < now() - %s"
            " ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED)"
            " AND status = ANY(%s) AND updated_at < now() - %s RETURNING *",
            (list(statuses), older_than, list(statuses), older_than))

    async def find_by_merge_sha(self, *, app: str, target_env: str | None, image_tag: str) -> dict[str, Any] | None:
        if len(image_tag) < 7:
            return None
        return await self._fetchone(
            "SELECT * FROM reviews WHERE app = %s AND (%s::text IS NULL OR target_env = %s) AND merge_sha IS NOT NULL"
            " AND (%s::text IS NOT NULL OR deployment_request_id IS NULL) AND starts_with(lower(merge_sha), lower(%s)) ORDER BY created_at DESC LIMIT 1",
            (app, target_env, target_env, target_env, image_tag),
        )

    async def find_by_merge_sha_exact(self, merge_sha: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM reviews WHERE merge_sha = %s ORDER BY created_at DESC LIMIT 1", (merge_sha,))

    async def latest_merged_review(self, app: str, target_env: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM reviews WHERE app = %s AND target_env = %s AND merge_sha IS NOT NULL"
            " ORDER BY created_at DESC LIMIT 1", (app, target_env))

    async def get_baseline(self, app: str, target_env: str) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM baselines WHERE app=%s AND target_env=%s", (app, target_env))
        if row and not await self.merge_has_failed(app, target_env, row["merge_sha"]):
            return row
        # A rollout can become Degraded after Healthy; recover the last known successful version.
        fallback = await self._fetchone(
            "SELECT r.app,r.target_env,r.final_spec AS spec,r.spec_ref,r.merge_sha,"
            " NULL::boolean AS database_has_data,d.received_at AS observed_at FROM reviews r"
            " JOIN (SELECT review_id,target_env,received_at FROM deploy_events WHERE kind='healthy' UNION ALL"
            " SELECT review_id,target_env,received_at FROM deployment_observations WHERE kind='deployed'"
            " AND payload->>'health'='Healthy' AND payload->>'sync_status'='Synced'"
            " AND payload->'operation'->>'phase'='Succeeded') d ON d.review_id=r.review_id AND d.target_env=r.target_env"
            " WHERE r.app=%s AND r.target_env=%s AND r.final_spec IS NOT NULL"
            f" AND ({LAST_DEPLOY_FAILED}) IS NOT TRUE"
            " ORDER BY r.created_at DESC LIMIT 1", (app, target_env))
        return {**fallback, "derived": True} if fallback else None

    async def merge_has_failed(self, app, target_env, merge_sha):
        if not merge_sha:
            return False
        rows = await self._fetchall("SELECT review_id FROM reviews WHERE app=%s AND target_env=%s AND merge_sha=%s",
                                    (app, target_env, merge_sha))
        return any([await self.is_deployment_failing(r["review_id"]) for r in rows])

    async def upsert_baseline(self, *, app: str, target_env: str, spec: dict[str, Any], spec_ref: dict[str, Any],
                              merge_sha: str | None, observed_at: datetime) -> None:
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (app + "/" + target_env,))
            await cur.execute("SELECT 1 FROM reviews r WHERE r.app=%s AND r.target_env=%s AND r.merge_sha=%s"
                              f" AND ({LAST_DEPLOY_FAILED}) IS TRUE LIMIT 1", (app,target_env,merge_sha))
            if await cur.fetchone():
                return
            # Refuse a stale success atomically, including concurrent webhook replicas.
            await cur.execute("SELECT r.created_at FROM baselines b JOIN reviews r ON r.merge_sha=b.merge_sha"
                              " AND r.app=b.app AND r.target_env=b.target_env WHERE b.app=%s AND b.target_env=%s",
                              (app,target_env))
            current = await cur.fetchone()
            await cur.execute("SELECT created_at FROM reviews WHERE app=%s AND target_env=%s AND merge_sha=%s ORDER BY created_at DESC LIMIT 1",
                              (app,target_env,merge_sha))
            incoming = await cur.fetchone()
            if current and incoming and current["created_at"] > incoming["created_at"]:
                return
            await cur.execute(
                "INSERT INTO baselines (app,target_env,spec,spec_ref,merge_sha,database_has_data,observed_at)"
                " VALUES (%s,%s,%s,%s,%s,NULL,%s) ON CONFLICT(app,target_env) DO UPDATE SET spec=EXCLUDED.spec,"
                " spec_ref=EXCLUDED.spec_ref,merge_sha=EXCLUDED.merge_sha,database_has_data=NULL,observed_at=EXCLUDED.observed_at"
                " WHERE baselines.merge_sha IS DISTINCT FROM EXCLUDED.merge_sha",
                (app,target_env,Jsonb(spec),Jsonb(spec_ref),merge_sha,observed_at))

    async def add_deploy_event(self, *, review_id: str | None, app: str, target_env: str, kind: str,
                               image_tag: str | None, payload: dict[str, Any]) -> None:
        await self._execute(
            "INSERT INTO deploy_events (review_id, app, target_env, kind, image_tag, payload)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (review_id, app, target_env, kind, image_tag, Jsonb(payload)),
        )

    async def has_deploy_event(self, *, review_id: str, target_env: str, kind: str, image_tag: str | None) -> bool:
        return await self._fetchone(
            "SELECT 1 AS hit FROM deploy_events WHERE review_id = %s AND target_env = %s AND kind = %s"
            " AND image_tag IS NOT DISTINCT FROM %s LIMIT 1", (review_id, target_env, kind, image_tag)) is not None

    async def last_deploy_event(self, *, review_id: str, target_env: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT kind, image_tag, received_at FROM deploy_events WHERE review_id = %s AND target_env = %s"
            " ORDER BY received_at DESC, id DESC LIMIT 1", (review_id, target_env))

    async def deploy_events_for(self, review_id: str) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT id, review_id, app, target_env, kind, image_tag, payload, received_at FROM deploy_events"
            " WHERE review_id = %s ORDER BY received_at, id", (review_id,))

    async def find_superseding_parent(self, review_id: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM reviews WHERE superseded_by = %s ORDER BY created_at DESC LIMIT 1", (review_id,))

    async def baseline_or_last_committed(self, app: str, target_env: str) -> dict[str, Any] | None:
        return await self.get_baseline(app, target_env) or await self._fetchone(
            f"SELECT {_COMMITTED_AS_BASELINE} FROM reviews r"
            " WHERE app = %s AND target_env = %s AND status = 'committed' AND final_spec IS NOT NULL"
            f" AND ({LAST_DEPLOY_FAILED}) IS NOT TRUE"
            " ORDER BY updated_at DESC LIMIT 1", (app, target_env))

    async def latest_baseline_for_repository(self, repository: str) -> dict[str, Any] | None:
        scopes = await self._fetchall("SELECT app,target_env FROM reviews WHERE spec_ref->>'repository'=%s UNION"
                                     " SELECT app,target_env FROM baselines WHERE spec_ref->>'repository'=%s", (repository,repository))
        rows = [await self.get_baseline(scope["app"],scope["target_env"]) for scope in scopes]
        rows = [b for b in rows if b and b["spec_ref"].get("repository") == repository]
        if rows:
            return max(rows, key=lambda b: b["observed_at"] or datetime.min.replace(tzinfo=UTC))
        return await self._fetchone(
            f"SELECT {_COMMITTED_AS_BASELINE} FROM reviews r"
            " WHERE spec_ref->>'repository' = %s AND status = 'committed' AND final_spec IS NOT NULL"
            f" AND ({LAST_DEPLOY_FAILED}) IS NOT TRUE"
            " ORDER BY updated_at DESC LIMIT 1", (repository,))

    async def insert_intake(self, **intake: Any) -> bool:
        _check_intake(intake, {})
        columns = list(INTAKE_FIELDS)
        values = [Jsonb(intake[c]) if c in INTAKE_JSON_COLUMNS else intake[c] for c in columns]
        return await self._execute(
            f"INSERT INTO spec_intakes ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
            " ON CONFLICT (repository, head_sha) DO NOTHING", values) == 1

    async def get_intake(self, intake_id: str) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM spec_intakes WHERE intake_id = %s", (intake_id,))

    async def find_intake_by_head(self, repository: str, sha: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM spec_intakes WHERE repository = %s AND head_sha = %s", (repository, sha))

    async def latest_intake_by_head_sha(self, sha: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM spec_intakes WHERE head_sha = %s ORDER BY created_at DESC LIMIT 1", (sha,))

    async def find_intake_by_result_commit(self, repository: str, sha: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM spec_intakes WHERE repository = %s AND result_commit_sha = %s"
            " ORDER BY created_at DESC LIMIT 1", (repository, sha))

    async def find_intake_by_reviews(self, review_ids: Sequence[str]) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM spec_intakes WHERE review_id = ANY(%s) ORDER BY created_at DESC LIMIT 1", (list(review_ids),))

    async def finish_intake(self, intake_id: str, **fields: Any) -> bool:
        _check_intake(dict.fromkeys(INTAKE_FIELDS), fields)
        columns = sorted(fields)
        values = [Jsonb(fields[c]) if c in INTAKE_JSON_COLUMNS else fields[c] for c in columns]
        assignments = ", ".join(f"{c} = %s" for c in columns)
        return await self._execute(
            f"UPDATE spec_intakes SET {assignments}, updated_at = now()"
            " WHERE intake_id = %s AND status = 'processing'", (*values, intake_id)) == 1

    async def link_intake(self, intake_id: str, *, result_commit_sha: str | None = None,
                          review_id: str | None = None, baseline_used: bool | None = None,
                          unverified_paths: Sequence[str] | None = None) -> str | None:
        paths = list(unverified_paths) if unverified_paths is not None else None
        row = await self._fetchone(
            "UPDATE spec_intakes SET result_commit_sha = COALESCE(result_commit_sha, %s),"
            " review_id = COALESCE(%s, review_id), baseline_used = COALESCE(baseline_used, %s),"
            " unverified_paths = COALESCE(unverified_paths, %s::text[]),"
            " updated_at = now() WHERE intake_id = %s RETURNING result_commit_sha",
            (result_commit_sha, review_id, baseline_used, paths, intake_id))
        return row["result_commit_sha"] if row else None

    async def touch_intake(self, intake_id: str) -> None:
        await self._execute("UPDATE spec_intakes SET updated_at = now() WHERE intake_id = %s AND status = 'processing'",
                            (intake_id,))

    async def unverified_generation_for_pr(self, repository: str, pr_number: int) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM spec_intakes WHERE repository = %s AND pr_number = %s AND kind = ANY(%s)"
            " AND result_commit_sha IS NOT NULL AND baseline_used IS NOT TRUE"
            " AND (unverified_paths IS NULL OR cardinality(unverified_paths) > 0) ORDER BY created_at DESC LIMIT 1",
            (repository, pr_number, list(GENERATED_KINDS)))

    async def claim_stale_intakes(self, older_than: timedelta) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "UPDATE spec_intakes SET updated_at = now(), attempts = attempts + 1"
            " WHERE status = 'processing' AND updated_at < now() - %s RETURNING *", (older_than,))
        return sorted(rows, key=lambda r: r["created_at"])

    async def insert_case(self, **case: Any) -> bool:
        _check_case(case)
        columns = list(CASE_FIELDS)
        values = [Jsonb(case[c]) if c == "ops" else case[c] for c in columns]
        return await self._execute(
            f"INSERT INTO review_cases ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
            " ON CONFLICT (case_id) DO NOTHING", values) == 1

    async def find_cases(self, rule_id: str, *, app: str, repository: str, target_env: str,
                         outcomes: Sequence[str], limit: int) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT c.* FROM review_cases c JOIN reviews r USING (review_id)"
            " WHERE c.rule_ids @> ARRAY[%s]::text[] AND c.app = %s AND r.spec_ref->>'repository' = %s"
            " AND c.outcome = ANY(%s)"
            " ORDER BY (c.target_env = %s) DESC, c.created_at DESC LIMIT %s",
            (rule_id, app, repository, list(outcomes), target_env, limit))


class InMemoryReviewRepository(MemoryRequests, MemoryDeployments):
    """테스트·DB 없는 로컬 실행용. Postgres 구현과 같은 규칙으로 동작한다."""

    def __init__(self) -> None:
        self.requests = {}
        self.request_locks = {}
        self.request_retries = {}
        self.reviews: dict[str, dict[str, Any]] = {}
        self.baselines: dict[tuple[str, str], dict[str, Any]] = {}
        self.deploy_events: list[dict[str, Any]] = []
        self.intakes: dict[str, dict[str, Any]] = {}
        self.cases: dict[str, dict[str, Any]] = {}

    async def insert_review(self, *, review_id: str, app: str, target_env: str, repo_id: str,
                            spec_ref: dict[str, Any], pr_head_sha: str, requested_by: str,
                            pr_number: int | None = None, deployment_request_id: str | None = None) -> str:
        if review_id in self.reviews:
            raise ValueError(f"review_id 중복: {review_id}")
        for r in self.reviews.values():
            if ((deployment_request_id is not None and r.get("deployment_request_id") == deployment_request_id
                 and r["target_env"] == target_env) or
                (deployment_request_id is None and r.get("deployment_request_id") is None and
                 r["repo_id"] == repo_id and r["pr_head_sha"] == pr_head_sha and r["status"] not in HEAD_REUSABLE)):
                return r["review_id"]
        now = _now()
        self.reviews[review_id] = {
            "review_id": review_id, "app": app, "target_env": target_env, "repo_id": repo_id,
            "spec_ref": copy.deepcopy(spec_ref), "pr_head_sha": pr_head_sha, "merge_sha": None,
            "status": "received", "verdict": None, "reasons": [], "decision": None, "findings": None,
            "rounds": None, "human_decision": None, "deploy_result": None, "gitops_commit_sha": None,
            "final_spec": None, "error": None, "superseded_by": None, "requested_by": requested_by,
            "pr_number": pr_number, "deployment_request_id": deployment_request_id, "recover_count": 0, **dict.fromkeys(STAGE_TIMES), "created_at": now,
            "updated_at": now,
        }
        return review_id

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

    async def list_reviews(self, statuses: Sequence[str], limit: int) -> list[dict[str, Any]]:
        rows = [r for r in self.reviews.values() if not statuses or r["status"] in statuses]
        rows.sort(key=lambda r: (r["created_at"], r["review_id"]), reverse=True)
        return [copy.deepcopy({k: r[k] for k in LIST_FIELDS}) for r in rows[:limit]]

    async def find_by_head(self, repository: str, sha: str) -> dict[str, Any] | None:
        return self._latest(r for r in self.reviews.values() if r["spec_ref"].get("repository") == repository
                            and r["pr_head_sha"] == sha and r["status"] != "failed")

    async def supersede_open(self, *, repository: str, pr_number: int, superseded_by: str | None,
                             error: str | None = None) -> list[str]:
        done = []
        for r in sorted(self.reviews.values(), key=lambda r: r["created_at"]):
            if (r["spec_ref"].get("repository") == repository and r["pr_number"] == pr_number
                    and r["review_id"] != superseded_by and r["status"] in OPEN):
                r.update(status="superseded", superseded_by=superseded_by, error=error or r["error"], updated_at=_now())
                done.append(r["review_id"])
        return done

    async def waiting_ci_by_head_sha(self, sha: str) -> list[dict[str, Any]]:
        rows = [r for r in self.reviews.values() if r["pr_head_sha"] == sha and r["status"] == "waiting_ci"]
        return copy.deepcopy(sorted(rows, key=lambda r: r["created_at"]))

    async def status_counts(self, since: timedelta) -> list[dict[str, Any]]:
        counts: dict[tuple[str, str, str], int] = {}
        for r in self.reviews.values():
            if r["updated_at"] > _now() - since:
                key = (r["app"], r["target_env"], r["status"])
                counts[key] = counts.get(key, 0) + 1
        return [{"app": a, "target_env": e, "status": st, "count": n} for (a, e, st), n in counts.items()]

    async def needs_human_oldest(self) -> list[dict[str, Any]]:
        oldest: dict[tuple[str, str], datetime] = {}
        for r in self.reviews.values():
            if r["status"] == "needs_human":
                key = (r["app"], r["target_env"])
                oldest[key] = min(oldest.get(key, r["updated_at"]), r["updated_at"])
        now = _now()
        return [{"app": a, "target_env": e, "seconds": (now - t).total_seconds()} for (a, e), t in oldest.items()]

    async def claim_stale_review(self, older_than: timedelta, statuses: Sequence[str]) -> dict[str, Any] | None:
        now = _now()
        rows = [r for r in self.reviews.values() if r["status"] in statuses and r["updated_at"] < now - older_than]
        if not rows:
            return None
        row = min(rows, key=lambda r: r["updated_at"])
        row.update(recover_count=row["recover_count"] + 1, updated_at=now)
        return copy.deepcopy(row)

    async def find_by_merge_sha(self, *, app: str, target_env: str | None, image_tag: str) -> dict[str, Any] | None:
        return self._latest(r for r in self.reviews.values()
                            if r["app"] == app and target_env in (None, r["target_env"])
                            and (target_env is not None or r.get("deployment_request_id") is None)
                            and tag_matches(r["merge_sha"], image_tag))

    async def find_by_merge_sha_exact(self, merge_sha: str) -> dict[str, Any] | None:
        return self._latest(r for r in self.reviews.values() if r["merge_sha"] == merge_sha)

    async def latest_merged_review(self, app: str, target_env: str) -> dict[str, Any] | None:
        return self._latest(r for r in self.reviews.values()
                            if r["app"] == app and r["target_env"] == target_env and r["merge_sha"])

    async def get_baseline(self, app: str, target_env: str) -> dict[str, Any] | None:
        row = self.baselines.get((app,target_env))
        if row and not await self.merge_has_failed(app,target_env,row["merge_sha"]):
            return copy.deepcopy(row)
        self._deployment_memory()
        healthy_ids = {e["review_id"] for e in self.deploy_events if e["kind"] == "healthy" and e["target_env"] == target_env}
        healthy_ids |= {e["review_id"] for e in self.observations.values() if e["kind"] == "deployed" and e["target_env"] == target_env
                        and e["payload"].get("health") == "Healthy" and e["payload"].get("sync_status") == "Synced"
                        and (e["payload"].get("operation") or {}).get("phase") == "Succeeded"}
        rows = [r for r in self.reviews.values() if r["review_id"] in healthy_ids and r["app"] == app
                and r["target_env"] == target_env and r["final_spec"] and not await self.is_deployment_failing(r["review_id"])]
        last = self._latest(rows)
        return dict(app=app,target_env=target_env,spec=last["final_spec"],spec_ref=last["spec_ref"],merge_sha=last["merge_sha"],
                    database_has_data=None,observed_at=None,derived=True) if last else None

    async def merge_has_failed(self, app, target_env, merge_sha):
        return any([await self.is_deployment_failing(r["review_id"]) for r in self.reviews.values()
                    if r["app"] == app and r["target_env"] == target_env and merge_sha and r["merge_sha"] == merge_sha])

    async def upsert_baseline(self, *, app: str, target_env: str, spec: dict[str, Any], spec_ref: dict[str, Any],
                              merge_sha: str | None, observed_at: datetime) -> None:
        if await self.merge_has_failed(app,target_env,merge_sha):
            return
        current = self.baselines.get((app,target_env))
        if current:
            if current["merge_sha"] == merge_sha:
                return
            old = await self.find_by_merge_sha_exact(current["merge_sha"])
            new = await self.find_by_merge_sha_exact(merge_sha)
            if old and new and old["created_at"] > new["created_at"]:
                return
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

    async def has_deploy_event(self, *, review_id: str, target_env: str, kind: str, image_tag: str | None) -> bool:
        return any(e["review_id"] == review_id and e["target_env"] == target_env and e["kind"] == kind
                   and e["image_tag"] == image_tag for e in self.deploy_events)

    async def last_deploy_event(self, *, review_id: str, target_env: str) -> dict[str, Any] | None:
        rows = [e for e in self.deploy_events if e["review_id"] == review_id and e["target_env"] == target_env]
        last = max(rows, key=lambda e: (e["received_at"], e["id"]), default=None)
        return {k: last[k] for k in ("kind", "image_tag", "received_at")} if last else None

    async def deploy_events_for(self, review_id: str) -> list[dict[str, Any]]:
        rows = [e for e in self.deploy_events if e["review_id"] == review_id]
        return copy.deepcopy(sorted(rows, key=lambda e: (e["received_at"], e["id"])))

    async def find_superseding_parent(self, review_id: str) -> dict[str, Any] | None:
        return self._latest(r for r in self.reviews.values() if r["superseded_by"] == review_id)

    async def latest_baseline_for_repository(self, repository: str) -> dict[str, Any] | None:
        scopes = {(r["app"],r["target_env"]) for r in self.reviews.values() if r["spec_ref"].get("repository") == repository}
        scopes |= {(b["app"],b["target_env"]) for b in self.baselines.values() if b["spec_ref"].get("repository") == repository}
        rows = [await self.get_baseline(app,env) for app,env in scopes]
        rows = [r for r in rows if r and r["spec_ref"].get("repository") == repository]
        rows.sort(key=lambda b: b["observed_at"] or datetime.min.replace(tzinfo=UTC))
        if rows:
            return copy.deepcopy(rows[-1])
        return await self._last_committed_as_baseline(lambda r: r["spec_ref"].get("repository") == repository)

    async def baseline_or_last_committed(self, app: str, target_env: str) -> dict[str, Any] | None:
        return await self.get_baseline(app, target_env) or await self._last_committed_as_baseline(
            lambda r: r["app"] == app and r["target_env"] == target_env)

    async def _last_committed_as_baseline(self, match: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        committed = [r for r in self.reviews.values()
                     if match(r) and r["status"] == "committed" and r["final_spec"] is not None
                     and not await self.is_deployment_failing(r["review_id"])]
        if not committed:
            return None
        last = max(committed, key=lambda r: r["updated_at"])
        return copy.deepcopy({"app": last["app"], "target_env": last["target_env"], "spec": last["final_spec"],
                              "spec_ref": last["spec_ref"], "merge_sha": last["merge_sha"],
                              "database_has_data": None, "observed_at": None})

    async def insert_intake(self, **intake: Any) -> bool:
        _check_intake(intake, {})
        if await self.find_intake_by_head(intake["repository"], intake["head_sha"]) is not None:
            return False
        now = _now()
        self.intakes[intake["intake_id"]] = {
            **copy.deepcopy(intake), "status": "processing", "reason": None, "message": None, "details": [],
            "result_commit_sha": None, "review_id": None, "baseline_used": None, "unverified_paths": None,
            "attempts": 1, "created_at": now, "updated_at": now,
        }
        return True

    async def get_intake(self, intake_id: str) -> dict[str, Any] | None:
        row = self.intakes.get(intake_id)
        return copy.deepcopy(row) if row else None

    async def find_intake_by_head(self, repository: str, sha: str) -> dict[str, Any] | None:
        return self._latest(r for r in self.intakes.values() if r["repository"] == repository and r["head_sha"] == sha)

    async def latest_intake_by_head_sha(self, sha: str) -> dict[str, Any] | None:
        return self._latest(r for r in self.intakes.values() if r["head_sha"] == sha)

    async def find_intake_by_result_commit(self, repository: str, sha: str) -> dict[str, Any] | None:
        return self._latest(r for r in self.intakes.values()
                            if r["repository"] == repository and r["result_commit_sha"] == sha)

    async def find_intake_by_reviews(self, review_ids: Sequence[str]) -> dict[str, Any] | None:
        return self._latest(r for r in self.intakes.values() if r["review_id"] in review_ids)

    async def finish_intake(self, intake_id: str, **fields: Any) -> bool:
        _check_intake(dict.fromkeys(INTAKE_FIELDS), fields)
        row = self.intakes.get(intake_id)
        if row is None or row["status"] != "processing":
            return False
        row.update(copy.deepcopy(fields), updated_at=_now())
        return True

    async def link_intake(self, intake_id: str, *, result_commit_sha: str | None = None,
                          review_id: str | None = None, baseline_used: bool | None = None,
                          unverified_paths: Sequence[str] | None = None) -> str | None:
        row = self.intakes.get(intake_id)
        if row is None:
            return None
        paths = list(unverified_paths) if unverified_paths is not None and row["unverified_paths"] is None else None
        links = {"result_commit_sha": row["result_commit_sha"] or result_commit_sha, "review_id": review_id,
                 "baseline_used": baseline_used if row["baseline_used"] is None else None, "unverified_paths": paths}
        row.update({k: v for k, v in links.items() if v is not None}, updated_at=_now())
        return row["result_commit_sha"]

    async def touch_intake(self, intake_id: str) -> None:
        row = self.intakes.get(intake_id)
        if row is not None and row["status"] == "processing":
            row["updated_at"] = _now()

    async def unverified_generation_for_pr(self, repository: str, pr_number: int) -> dict[str, Any] | None:
        return self._latest(r for r in self.intakes.values()
                            if r["repository"] == repository and r["pr_number"] == pr_number
                            and r["kind"] in GENERATED_KINDS and r["result_commit_sha"] is not None
                            and r.get("baseline_used") is not True and r.get("unverified_paths") != [])

    async def claim_stale_intakes(self, older_than: timedelta) -> list[dict[str, Any]]:
        now = _now()
        rows = [r for r in self.intakes.values() if r["status"] == "processing" and r["updated_at"] < now - older_than]
        for r in rows:
            r.update(updated_at=now, attempts=r["attempts"] + 1)
        return copy.deepcopy(sorted(rows, key=lambda r: r["created_at"]))

    async def insert_case(self, **case: Any) -> bool:
        _check_case(case)
        if case["case_id"] in self.cases:
            return False
        self.cases[case["case_id"]] = {**copy.deepcopy(case), "created_at": _now()}
        return True

    async def find_cases(self, rule_id: str, *, app: str, repository: str, target_env: str,
                         outcomes: Sequence[str], limit: int) -> list[dict[str, Any]]:
        def matches(c: dict[str, Any]) -> bool:
            review = self.reviews.get(c["review_id"]) or {}
            return (rule_id in c["rule_ids"] and c["app"] == app and c["outcome"] in outcomes
                    and (review.get("spec_ref") or {}).get("repository") == repository)

        rows = sorted((c for c in reversed(self.cases.values()) if matches(c)),
                      key=lambda c: c["created_at"], reverse=True)  # 같은 시각이면 나중에 넣은 것이 앞
        rows.sort(key=lambda c: c["target_env"] != target_env)  # 안정 정렬 — 같은 환경 안에서는 최근 순서 유지
        return copy.deepcopy(rows[:limit])


def make_pool(conninfo: str, *, min_size: int = 1, max_size: int = 5) -> AsyncConnectionPool:
    """autocommit 풀. LangGraph AsyncPostgresSaver 도 같은 설정(autocommit·dict_row)을 요구한다."""
    return AsyncConnectionPool(conninfo, min_size=min_size, max_size=max_size, open=False,
                               kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
