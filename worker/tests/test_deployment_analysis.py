from datetime import UTC, datetime, timedelta
import json

import pytest

from review_ai.judge.llm import LlmResponse
from review_ai.errors import TransientError
from review_common.deployment import AnalysisRequested, dispatch_pending
from review_common.repository import InMemoryReviewRepository
from review_worker.deployment_analysis import DeploymentAnalysisHandler
from tests.conftest import load_sample


EID = "de_" + "d"*64


async def record(repo, spec=None):
    await repo.record_deployment(dict(event_id=EID, attempt_key="attempt", app="sample-app", target_env="aws",
        kind="sync_failed", review_id=None, repository="org/app" if spec else None, spec_snapshot=spec,
        payload={"operation": {"phase": "Failed", "message": "resource forbidden"}}, occurred_at=datetime.now(UTC)))


class Llm:
    calls = 0
    async def complete(self, req):
        self.calls += 1
        return LlmResponse(json.dumps(dict(summary="권한 오류 관측", hypotheses=[dict(reason="배포 권한 부족 후보",
            evidence_ids=["e2"], spec_paths=[], actions=["운영 담당자에게 리소스 적용 권한 확인 요청"])],
            additional_information=[], cited_case_ids=[])), "fake")


async def test_isolated_analysis_duplicate_consumption_and_missing_spec():
    repo = InMemoryReviewRepository(); await record(repo)
    llm = Llm(); handler = DeploymentAnalysisHandler(repo, llm)
    value = AnalysisRequested(event_id=EID).model_dump_json().encode()
    await handler.handle(value); await handler.handle(value)
    event = await repo.get_deployment(EID)
    assert event["analysis_status"] == "completed" and llm.calls == 1
    assert event["result"]["evidence"] and repo.reviews == {}
    assert await repo.get_failure_case(EID) is None


async def test_bounded_retries_and_stale_lease_recovery():
    repo = InMemoryReviewRepository(); await record(repo)
    class Broken:
        async def complete(self, req):
            raise TransientError("unavailable")
    handler = DeploymentAnalysisHandler(repo, Broken())
    raw = AnalysisRequested(event_id=EID).model_dump_json().encode()
    for _ in range(3):
        await handler.handle(raw)
    assert (await repo.get_deployment(EID))["analysis_status"] == "failed"
    assert repo.analysis_jobs[EID]["attempts"] == 3
    await handler.handle(raw)
    assert repo.analysis_jobs[EID]["attempts"] == 3

    repo = InMemoryReviewRepository(); await record(repo)
    old = await repo.claim_analysis(EID)
    repo.analysis_jobs[EID]["lease_until"] = datetime.now(UTC)-timedelta(seconds=1)
    new = await repo.claim_analysis(EID)
    assert old["lease_token"] != new["lease_token"]
    assert not await repo.finish_analysis(EID, old["lease_token"], status="completed", result={"bad": True})
    assert await repo.finish_analysis(EID, new["lease_token"], status="insufficient", result={})


async def test_publish_failure_does_not_lose_job_and_queued_is_redelivered():
    repo = InMemoryReviewRepository(); await record(repo)
    class Publisher:
        broken = True
        calls = []
        async def send(self, topic, key, value):
            if self.broken:
                raise RuntimeError("broker down")
            self.calls.append(value)
    publisher = Publisher()
    assert await dispatch_pending(repo, publisher) == 0
    assert repo.analysis_jobs[EID]["status"] == "queued"
    publisher.broken = False
    repo.analysis_jobs[EID]["next_publish_at"] = datetime.now(UTC)-timedelta(seconds=1)
    assert await dispatch_pending(repo, publisher) == 1
    assert len(publisher.calls) == 1
    assert await dispatch_pending(repo, publisher) == 0


async def test_invalid_output_is_visible_and_failure_case_is_retained(caplog):
    repo = InMemoryReviewRepository()
    spec = {"metadata": {"name": "sample-app"}, "target": {"env": "aws"}}
    await record(repo, spec)
    class Invalid:
        async def complete(self, req):
            return LlmResponse('{"not":"diagnosis"}', "fake")
    await DeploymentAnalysisHandler(repo, Invalid()).handle(AnalysisRequested(event_id=EID).model_dump_json().encode())
    event = await repo.get_deployment(EID)
    assert event["analysis_status"] == "failed" and event["error_code"] == "ANALYSIS_INVALID"
    assert (await repo.get_failure_case(EID))["diagnosis"]["evidence"]
    invalid = [r.getMessage() for r in caplog.records if "배포 분석 결과 무효" in r.getMessage()]
    assert len(invalid) == 1 and EID in invalid[0] and "error=" in invalid[0]


async def test_static_pass_still_runs_case_advice_and_keeps_original_verdict(harness):
    spec = load_sample("01-pass-sample-app-aws.yaml")
    await harness.repo.save_failure_case(dict(case_id="old-failure", app=spec["metadata"]["name"],
        repository=spec["metadata"]["repository"], target_env=spec["target"]["env"], failed_spec=spec,
        diagnosis=dict(summary="과거 probe 실패", spec_paths=["/runtime/health/readiness"],
                       hypotheses=[dict(actions=["probe 경로 확인"])])))
    await harness.request(spec)
    row = harness.row()
    assert row["verdict"] == "pass"
    assert row["case_advice"]["status"] == "completed"
    assert row["case_advice"]["items"][0]["applicability"] == "applicable"


async def test_case_advice_recompares_final_spec_after_existing_auto_fix(harness):
    spec = load_sample("05-fix-engine-unsupported-local.yaml")
    await harness.repo.save_failure_case(dict(case_id="engine-failure", app=spec["metadata"]["name"],
        repository=spec["metadata"]["repository"], target_env=spec["target"]["env"], failed_spec=spec,
        diagnosis=dict(summary="엔진 지원 실패", spec_paths=["/database/engine"], hypotheses=[])))
    await harness.repo.confirm_resolution("engine-failure", dict(cause="엔진 변경으로 해결 확인", actions=["엔진 변경"],
        conditions=[dict(path="/database/engine", failed_value=spec["database"]["engine"], resolved_value="postgres")]))
    await harness.request(spec)
    row = harness.row()
    assert row["rounds"][0]["verdict"] == "fix"
    assert row["final_spec"]["database"]["engine"] == "postgres"
    assert row["case_advice"]["items"][0]["applicability"] == "resolved"
