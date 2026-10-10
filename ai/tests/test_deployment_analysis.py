import copy
import json

import pytest
from review_ai.deployment_analysis import case_advice, compare_case, config_value, diagnose, evidence_from
from review_ai.failure_evidence import safe_data
from review_ai.judge.llm import LlmResponse


SPEC = {"metadata": {"name": "app"}, "target": {"env": "gcp"},
        "runtime": {"health": {"readiness": "/wrong"}, "env": {"PASSWORD": "value"}}}


def case():
    return dict(case_id="case1", failed_spec=copy.deepcopy(SPEC), resolution=None,
                diagnosis={"summary": "probe 실패 원인 후보", "spec_paths": ["/runtime/health/readiness"],
                           "hypotheses": [{"actions": ["probe 경로 확인"]}]})


class Llm:
    def __init__(self, result, stop=None):
        self.result, self.stop = result, stop
    async def complete(self, request):
        self.request = request
        return LlmResponse(text=json.dumps(self.result), model="fake", stop_reason=self.stop)


def event():
    return dict(kind="health_degraded", spec_snapshot=SPEC,
                payload={"resources": [{"health": {"status": "Degraded", "message": "probe failed"}}]})


@pytest.mark.asyncio
async def test_diagnosis_validates_evidence_and_paths():
    answer = dict(summary="probe 실패", hypotheses=[dict(reason="경로 불일치 후보", evidence_ids=["e1"],
        spec_paths=["/runtime/health/readiness"], actions=["경로 확인"])], additional_information=[], cited_case_ids=[])
    llm = Llm(answer)
    status, result = await diagnose(event(), llm)
    assert status == "completed" and result["spec_paths"] == ["/runtime/health/readiness"]
    assert "신뢰할 수 없는 데이터" in llm.request.system
    assert "value" not in llm.request.user
    for invalid in ("e99",):
        answer["hypotheses"][0]["evidence_ids"] = [invalid]
        with pytest.raises(ValueError):
            await diagnose(event(), Llm(answer))
    answer["hypotheses"][0]["evidence_ids"] = ["e1"]
    answer["hypotheses"][0]["spec_paths"] = ["/runtime/env/PASSWORD"]
    with pytest.raises(ValueError):
        await diagnose(event(), Llm(answer))
    answer["hypotheses"][0]["spec_paths"] = []
    answer["cited_case_ids"] = ["invented"]
    with pytest.raises(ValueError):
        await diagnose(event(), Llm(answer))
    with pytest.raises(ValueError):
        await diagnose(event(), Llm(answer, "max_tokens"))


@pytest.mark.asyncio
async def test_insufficient_evidence_or_no_llm_preserves_failure():
    assert (await diagnose({"kind": "sync_failed", "payload": {}}, None))[0] == "insufficient"
    status, result = await diagnose(event(), None)
    assert status == "insufficient" and result["evidence"][0]["message"] == "probe failed"
    assert result["hypotheses"] == []


def test_only_verified_fix_conditions_suppress_repeat_warning():
    c = case()
    assert compare_case(c, SPEC)["applicability"] == "applicable"
    changed = copy.deepcopy(SPEC); changed["runtime"]["health"]["readiness"] = "/ready"
    assert compare_case(c, changed)["applicability"] == "unknown"  # changed alone is not proof
    c["resolution"] = dict(cause="검증된 경로 불일치", actions=["경로 수정"],
        conditions=[dict(path="/runtime/health/readiness", failed_value="/wrong", resolved_value="/ready")])
    assert compare_case(c, SPEC)["verified"]
    result = compare_case(c, changed)
    assert result["applicability"] == "resolved" and result["actions"] == []
    changed["runtime"]["health"]["readiness"] = "/another"
    assert compare_case(c, changed)["applicability"] == "unknown"
    del changed["runtime"]["health"]["readiness"]
    assert compare_case(c, changed)["applicability"] == "unknown"


@pytest.mark.asyncio
async def test_case_advice_never_requires_static_findings_and_reports_search_failure():
    class Repo:
        async def find_failure_cases(self, **kwargs):
            assert kwargs == dict(app="app", repository="org/app", target_env="gcp", limit=50)
            return [case()]
    result = await case_advice(Repo(), SPEC, "org/app")
    assert result["status"] == "completed" and result["items"][0]["case_id"] == "case1"
    class Down:
        async def find_failure_cases(self, **kwargs):
            raise RuntimeError("down")
    assert (await case_advice(Down(), SPEC, "org/app"))["status"] == "unavailable"


def test_error_redaction_and_multiple_evidence_sources():
    payload = dict(operation={"phase": "Error", "message": "password=hidden", "resources": [{"status": "SyncFailed", "message": "forbidden"}]},
                   conditions=[{"type": "SyncError", "message": "bad config"}], health_message="health failed",
                   resources=[{"health": {"status": "Degraded", "message": "probe failed"}}])
    evidence = evidence_from(payload)
    assert len(evidence) == 6 and "hidden" not in json.dumps(evidence)
    assert "anothersecret" not in json.dumps(safe_data({"message": "Bearer anothersecret"}))
    with pytest.raises(ValueError):
        config_value(SPEC, "/runtime/env/PASSWORD")
    with pytest.raises(ValueError):
        config_value(SPEC, "/runtime/health")


@pytest.mark.asyncio
async def test_observation_requires_verified_nonempty_changed_conditions():
    c = case()
    class Repo:
        async def find_failure_cases(self, **kwargs):
            return [c]
    assert (await case_advice(Repo(), SPEC, "org/app"))["observation"]["match_count"] == 0
    c["resolution"] = dict(confirmed_by="review-api-operator", success_event_id="success",
                           cause="fixed", conditions=[])
    assert (await case_advice(Repo(), SPEC, "org/app"))["observation"]["match_count"] == 0
    c["resolution"]["conditions"] = [dict(path="/runtime/health/readiness", failed_value="/wrong", resolved_value="/ready")]
    observed = (await case_advice(Repo(), SPEC, "org/app"))["observation"]
    assert observed["matched_case_ids"] == ["case1"] and observed["enforcement_ready"] is False
    fixed = copy.deepcopy(SPEC)
    fixed["runtime"]["health"]["readiness"] = "/ready"
    assert (await case_advice(Repo(), fixed, "org/app"))["observation"]["match_count"] == 0
    c["resolution"]["conditions"][0]["resolved_value"] = "/wrong"
    assert (await case_advice(Repo(), SPEC, "org/app"))["observation"]["match_count"] == 0


@pytest.mark.asyncio
async def test_observation_scans_beyond_display_limit_and_reports_search_cap():
    cases = [{**case(), "case_id": str(i)} for i in range(50)]
    cases[-1]["resolution"] = dict(confirmed_by="review-api-operator", success_event_id="success", cause="fixed",
        conditions=[dict(path="/runtime/health/readiness", failed_value="/wrong", resolved_value="/ready")])
    class Repo:
        async def find_failure_cases(self, **kwargs):
            return cases
    result = await case_advice(Repo(), SPEC, "org/app")
    assert len(result["items"]) == 5
    assert result["observation"]["matched_case_ids"] == ["49"]
    assert not result["observation"]["search_complete"]
    class Down:
        async def find_failure_cases(self, **kwargs):
            raise ConnectionError()
    unavailable = (await case_advice(Down(), SPEC, "org/app"))["observation"]
    assert unavailable["status"] == "unavailable" and unavailable["match_count"] is None
