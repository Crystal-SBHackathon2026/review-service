"""Persistence for immutable deployment evidence, a leased outbox, and scoped failure cases."""
from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from review_common.deployment import LEASE_SECONDS, MAX_ATTEMPTS, MAX_EVIDENCE_VERSIONS


FAILURE_KINDS = ("degraded", "sync_failed")
# 검토 대상 환경의 마지막 배포 알림이 실패인가 — "실패가 있었나"가 아니다. 실패 뒤 Healthy 가 오면 회복이다.
# deploy_events 에는 대상 환경의 실패가 전부 미러되고, v1 Healthy 는 검증된 성공(operation Succeeded)만 들어온다.
# reviews 별칭 r 에 붙여 쓴다. 알림이 없으면 NULL 이므로 IS TRUE·IS NOT TRUE 로 감싼다.
LAST_DEPLOY_FAILED = ("(SELECT d.kind FROM deploy_events d WHERE d.review_id=r.review_id AND d.target_env=r.target_env"
                      " ORDER BY d.received_at DESC,d.id DESC LIMIT 1) IN ('degraded','sync_failed')")


def initial_diagnosis():
    return dict(summary="배포 실패가 관측되었습니다. 원인 분석 전입니다.", hypotheses=[], spec_paths=[], evidence=[])


def now():
    return datetime.now(UTC)


class PostgresDeployments:
    async def mirror_deployment_event(self, *, event_id, review_id, app, target_env, kind, image_tag, payload):
        await self._execute(
            "INSERT INTO deploy_events(deployment_event_id,review_id,app,target_env,kind,image_tag,payload)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(deployment_event_id) DO NOTHING",
            (event_id, review_id, app, target_env, kind, image_tag, Jsonb(payload)))

    async def find_deployment_review(self, app, env, revision):
        if not revision:
            return None
        rows = await self._fetchall(
            "SELECT * FROM reviews WHERE app=%s AND (%s::text IS NULL OR target_env=%s) AND gitops_commit_sha=%s LIMIT 2",
            (app, env, env, revision))
        return rows[0] if len(rows) == 1 else None

    async def record_deployment(self, event):
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            # Serializes evidence limits and baseline decisions for this application.
            await cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                              (event["app"] + "/" + event["target_env"],))
            await cur.execute(
                "INSERT INTO deployment_observations (event_id,attempt_key,app,target_env,kind,review_id,repository,"
                "spec_snapshot,payload,occurred_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                (event["event_id"], event["attempt_key"], event["app"], event["target_env"], event["kind"],
                 event["review_id"], event["repository"], Jsonb(event["spec_snapshot"]), Jsonb(event["payload"]),
                 event["occurred_at"]))
            if await cur.fetchone() is None:
                return False
            if event["kind"] != "deployed":
                await cur.execute("SELECT count(*) AS n FROM deployment_observations WHERE attempt_key=%s"
                                  " AND kind != 'deployed'", (event["attempt_key"],))
                count = (await cur.fetchone())["n"]
                status = "pending" if count <= MAX_EVIDENCE_VERSIONS else "skipped"
                await cur.execute("INSERT INTO deployment_analysis_jobs(event_id,status,error_code) VALUES(%s,%s,%s)",
                                  (event["event_id"], status, "EVIDENCE_LIMIT" if status == "skipped" else None))
                if event["repository"] and event["spec_snapshot"]:
                    await cur.execute("INSERT INTO deployment_failure_cases(case_id,app,repository,target_env,failed_spec,diagnosis)"
                                      " VALUES(%s,%s,%s,%s,%s,%s)",
                                      (event["event_id"], event["app"], event["repository"], event["target_env"],
                                       Jsonb(event["spec_snapshot"]), Jsonb(initial_diagnosis())))
        return True

    async def get_deployment(self, event_id):
        return await self._fetchone(
            "SELECT o.*, j.status AS analysis_status,j.result,j.error_code,c.resolution FROM deployment_observations o"
            " LEFT JOIN deployment_analysis_jobs j USING(event_id) LEFT JOIN deployment_failure_cases c ON c.case_id=o.event_id"
            " WHERE o.event_id=%s", (event_id,))

    async def list_deployments(self, review_id=None, limit=50):
        return await self._fetchall(
            "SELECT o.*,j.status AS analysis_status,j.result,j.error_code FROM deployment_observations o"
            " LEFT JOIN deployment_analysis_jobs j USING(event_id) WHERE (%s::text IS NULL OR o.review_id=%s)"
            " ORDER BY o.occurred_at DESC,o.received_at DESC LIMIT %s", (review_id, review_id, limit))

    async def has_failed_deployment(self, review_id):
        return await self._fetchone(
            "SELECT 1 FROM reviews r WHERE r.review_id=%s AND ("
            " EXISTS(SELECT 1 FROM deployment_observations d WHERE d.review_id=r.review_id"
            " AND d.target_env=r.target_env AND d.kind != 'deployed')"
            " OR EXISTS(SELECT 1 FROM deploy_events d WHERE d.review_id=r.review_id"
            " AND d.target_env=r.target_env AND d.kind IN ('degraded','sync_failed'))) LIMIT 1",
            (review_id,)) is not None

    async def is_deployment_failing(self, review_id):
        return await self._fetchone(f"SELECT 1 FROM reviews r WHERE r.review_id=%s AND ({LAST_DEPLOY_FAILED}) IS TRUE",
                                    (review_id,)) is not None

    async def lease_analysis_publications(self):
        # Publish lease avoids a hot loop; successful publication is retried too until consumed.
        await self._execute(
            "UPDATE deployment_analysis_jobs SET status='failed',error_code='RETRIES_EXHAUSTED',updated_at=now()"
            " WHERE attempts >= %s AND status='processing' AND lease_until < now()", (MAX_ATTEMPTS,))
        return await self._fetchall(
            "WITH due AS (SELECT event_id FROM deployment_analysis_jobs WHERE next_publish_at<=now()"
            " AND (status IN ('pending','queued') OR (status='processing' AND lease_until<now()))"
            " AND attempts < %s ORDER BY next_publish_at LIMIT 50 FOR UPDATE SKIP LOCKED)"
            " UPDATE deployment_analysis_jobs j SET status='queued',next_publish_at=now()+interval '30 seconds',"
            " updated_at=now() FROM due WHERE j.event_id=due.event_id RETURNING j.event_id", (MAX_ATTEMPTS,))

    async def claim_analysis(self, event_id):
        token = uuid.uuid4().hex
        row = await self._fetchone(
            "UPDATE deployment_analysis_jobs SET status='processing',attempts=attempts+1,lease_token=%s,"
            " lease_until=now()+%s,updated_at=now() WHERE event_id=%s AND attempts<%s"
            " AND (status IN ('pending','queued') OR (status='processing' AND lease_until<now())) RETURNING *",
            (token, timedelta(seconds=LEASE_SECONDS), event_id, MAX_ATTEMPTS))
        return row

    async def finish_analysis(self, event_id, token, *, status, result=None, error_code=None, case=None):
        if status not in {"completed", "insufficient", "failed", "pending"}:
            raise ValueError("invalid analysis status")
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "UPDATE deployment_analysis_jobs SET status=%s,result=%s,error_code=%s,lease_until=NULL,"
                " next_publish_at=now()+interval '30 seconds',updated_at=now()"
                " WHERE event_id=%s AND status='processing' AND lease_token=%s",
                (status, Jsonb(result), error_code, event_id, token))
            if cur.rowcount != 1:
                return False
            if case:
                await cur.execute("UPDATE deployment_failure_cases SET diagnosis=%s WHERE case_id=%s",
                                  (Jsonb(case["diagnosis"]), event_id))
        return True

    async def save_failure_case(self, case):
        await self._execute(
            "INSERT INTO deployment_failure_cases(case_id,app,repository,target_env,failed_spec,diagnosis)"
            " VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(case_id) DO UPDATE SET diagnosis=EXCLUDED.diagnosis",
            (case["case_id"], case["app"], case["repository"], case["target_env"], Jsonb(case["failed_spec"]),
             Jsonb(case["diagnosis"])))

    async def get_failure_case(self, case_id):
        return await self._fetchone("SELECT * FROM deployment_failure_cases WHERE case_id=%s", (case_id,))

    async def find_failure_cases(self, *, app, repository, target_env, limit=20):
        return await self._fetchall(
            "SELECT c.*,o.attempt_key FROM deployment_failure_cases c JOIN deployment_observations o ON o.event_id=c.case_id"
            " WHERE c.app=%s AND c.repository=%s AND c.target_env=%s ORDER BY (c.resolution IS NOT NULL) DESC,c.created_at DESC LIMIT %s", (app, repository, target_env, min(limit, 50)))

    async def confirm_resolution(self, case_id, resolution):
        return await self._execute("UPDATE deployment_failure_cases SET resolution=%s WHERE case_id=%s"
                                   " AND resolution IS NULL", (Jsonb(resolution), case_id)) == 1


class MemoryDeployments:
    async def mirror_deployment_event(self, *, event_id, review_id, app, target_env, kind, image_tag, payload):
        if any(e.get("deployment_event_id") == event_id for e in self.deploy_events):
            return
        await self.add_deploy_event(review_id=review_id, app=app, target_env=target_env,
                                    kind=kind, image_tag=image_tag, payload=payload)
        self.deploy_events[-1]["deployment_event_id"] = event_id

    def _deployment_memory(self):
        if not hasattr(self, "observations"):
            self.observations = {}
            self.analysis_jobs = {}
            self.failure_cases = {}

    async def find_deployment_review(self, app, env, revision):
        rows = [r for r in self.reviews.values() if r["app"] == app and (env is None or r["target_env"] == env)
                and revision and r["gitops_commit_sha"] == revision]
        return copy.deepcopy(rows[0]) if len(rows) == 1 else None

    async def record_deployment(self, event):
        self._deployment_memory()
        eid = event["event_id"]
        if eid in self.observations:
            return False
        self.observations[eid] = copy.deepcopy({**event, "received_at": now()})
        if event["kind"] != "deployed":
            count = sum(e["attempt_key"] == event["attempt_key"] and e["kind"] != "deployed"
                        for e in self.observations.values())
            skipped = count > MAX_EVIDENCE_VERSIONS
            self.analysis_jobs[eid] = dict(event_id=eid, status="skipped" if skipped else "pending", attempts=0,
                                          lease_token=None, lease_until=None, next_publish_at=now(), result=None,
                                          error_code="EVIDENCE_LIMIT" if skipped else None)
            if event["repository"] and event["spec_snapshot"]:
                self.failure_cases[eid] = dict(case_id=eid, app=event["app"], repository=event["repository"],
                                               target_env=event["target_env"], failed_spec=copy.deepcopy(event["spec_snapshot"]),
                                               diagnosis=initial_diagnosis(), resolution=None, created_at=now(), attempt_key=event["attempt_key"])
        return True

    async def get_deployment(self, event_id):
        self._deployment_memory()
        row = self.observations.get(event_id)
        if row is None:
            return None
        job = self.analysis_jobs.get(event_id, {})
        return copy.deepcopy({**row, "analysis_status": job.get("status"), "result": job.get("result"),
                              "error_code": job.get("error_code"), "resolution": (self.failure_cases.get(event_id) or {}).get("resolution")})

    async def list_deployments(self, review_id=None, limit=50):
        self._deployment_memory()
        rows = sorted((r for r in self.observations.values() if review_id is None or r["review_id"] == review_id),
                      key=lambda r: (r["occurred_at"], r["received_at"]), reverse=True)
        return [await self.get_deployment(r["event_id"]) for r in rows[:limit]]

    async def has_failed_deployment(self, review_id):
        self._deployment_memory()
        row = self.reviews.get(review_id)
        if row is None:
            return False
        env = row["target_env"]
        return any(e["review_id"] == review_id and e["target_env"] == env and e["kind"] != "deployed"
                   for e in self.observations.values()) or any(
            e["review_id"] == review_id and e["target_env"] == env and e["kind"] in {"degraded", "sync_failed"}
            for e in self.deploy_events)

    async def is_deployment_failing(self, review_id):
        row = self.reviews.get(review_id)
        if row is None:
            return False
        last = await self.last_deploy_event(review_id=review_id, target_env=row["target_env"])
        return last is not None and last["kind"] in FAILURE_KINDS

    async def lease_analysis_publications(self):
        self._deployment_memory()
        out = []
        for j in self.analysis_jobs.values():
            stale = j["status"] == "processing" and j["lease_until"] < now()
            if stale and j["attempts"] >= MAX_ATTEMPTS:
                j.update(status="failed", error_code="RETRIES_EXHAUSTED")
            if j["next_publish_at"] <= now() and (j["status"] in {"pending", "queued"} or stale) and j["attempts"] < MAX_ATTEMPTS:
                j.update(status="queued", next_publish_at=now() + timedelta(seconds=30))
                out.append({"event_id": j["event_id"]})
                if len(out) == 50:
                    break
        return out

    async def claim_analysis(self, event_id):
        self._deployment_memory()
        j = self.analysis_jobs.get(event_id)
        if not j or j["attempts"] >= MAX_ATTEMPTS or not (j["status"] in {"pending", "queued"} or
                (j["status"] == "processing" and j["lease_until"] < now())):
            return None
        j.update(status="processing", attempts=j["attempts"] + 1, lease_token=uuid.uuid4().hex,
                 lease_until=now() + timedelta(seconds=LEASE_SECONDS))
        return copy.deepcopy(j)

    async def finish_analysis(self, event_id, token, *, status, result=None, error_code=None, case=None):
        if status not in {"completed", "insufficient", "failed", "pending"}:
            raise ValueError("invalid analysis status")
        j = self.analysis_jobs[event_id]
        if j["status"] != "processing" or j["lease_token"] != token:
            return False
        if case:
            await self.save_failure_case(case)
        j.update(status=status, result=copy.deepcopy(result), error_code=error_code, lease_until=None,
                 next_publish_at=now() + timedelta(seconds=30))
        return True

    async def save_failure_case(self, case):
        self._deployment_memory()
        previous = self.failure_cases.get(case["case_id"], {})
        self.failure_cases[case["case_id"]] = copy.deepcopy({**case, "created_at": previous.get("created_at", now()),
                                                          "resolution": previous.get("resolution"),
                                                          "attempt_key": previous.get("attempt_key", case["case_id"])})

    async def get_failure_case(self, case_id):
        self._deployment_memory()
        return copy.deepcopy(self.failure_cases.get(case_id))

    async def find_failure_cases(self, *, app, repository, target_env, limit=20):
        self._deployment_memory()
        rows = [c for c in self.failure_cases.values() if (c["app"], c["repository"], c["target_env"]) ==
                (app, repository, target_env)]
        return copy.deepcopy(sorted(rows, key=lambda c: (bool(c.get("resolution")), c["created_at"]), reverse=True)[:min(limit, 50)])

    async def confirm_resolution(self, case_id, resolution):
        self._deployment_memory()
        c = self.failure_cases.get(case_id)
        if c is None or c["resolution"] is not None:
            return False
        c["resolution"] = copy.deepcopy(resolution)
        return True
