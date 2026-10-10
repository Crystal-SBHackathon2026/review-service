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


FAIL_SPEC = {"metadata": {"name": "sample-app"}, "target": {"env": "aws"},
             "runtime": {"health": {"readiness": "/healthz"},
                         "env": {"DEPLOY_ENV": "aws", "FAIL_RATE": "0.3", "DB_PASSWORD": "hunter2"}}}


def fail_event():
    """sample-app#46 — FAIL_RATE 0.3 으로 카나리 중단. LLM 이 /runtime/env/FAIL_RATE 를 인용해 진단 전체가 무효였다."""
    return dict(kind="health_degraded", spec_snapshot=copy.deepcopy(FAIL_SPEC),
                payload={"operation": {"resources": [{"message": "rollout.argoproj.io/sample-app configured"}]}})


def hypothesis(*paths, reason="FAIL_RATE 환경 변수가 요청 일부를 실패시킨다", action="FAIL_RATE 를 제거한다"):
    return dict(reason=reason, evidence_ids=["e1"], spec_paths=list(paths), actions=[action])


def answer(*hyps, summary="카나리 에러율 초과"):
    return dict(summary=summary, hypotheses=list(hyps), additional_information=[], cited_case_ids=[])


@pytest.mark.asyncio
async def test_env_key_name_citation_is_allowed_without_value():
    """이번 케이스: /runtime/env/FAIL_RATE 인용은 키 이름까지 허용 — 값(0.3)은 결과 어디에도 없다."""
    llm = Llm(answer(hypothesis("/runtime/env/FAIL_RATE",
                                reason="FAIL_RATE 가 0.3 으로 설정돼 30% 가 500 이다",
                                action="FAIL_RATE 0.3 을 제거한다"),
                     summary="FAIL_RATE=0.3 때문에 에러율이 기준을 넘었다"))
    status, result = await diagnose(fail_event(), llm)
    assert status == "completed"
    assert result["spec_paths"] == ["/runtime/env/FAIL_RATE"]
    assert result["hypotheses"][0]["reason"] == "FAIL_RATE 가 (값 생략) 으로 설정돼 30% 가 500 이다"
    assert result["summary"] == "FAIL_RATE=(값 생략) 때문에 에러율이 기준을 넘었다"
    dumped = json.dumps(result, ensure_ascii=False)
    assert "0.3" not in dumped and "hunter2" not in dumped


@pytest.mark.asyncio
async def test_env_value_mask_keeps_similar_numbers():
    llm = Llm(answer(hypothesis("/runtime/env/FAIL_RATE", reason="FAIL_RATE 0.3 · 응답 10.3초 · 비율 0.35")))
    _, result = await diagnose(fail_event(), llm)
    assert result["hypotheses"][0]["reason"] == "FAIL_RATE (값 생략) · 응답 10.3초 · 비율 0.35"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/runtime/env/DB_PASSWORD", "/runtime/env/NOT_THERE", "/runtime/env",
                                  "/runtime/env/FAIL_RATE/x", "/metadata/name"])
async def test_unsupported_paths_only_drop_their_hypothesis(path):
    """비밀 이름·없는 키·env 전체·잘못된 경로를 든 가설만 버리고 나머지 진단은 살린다."""
    good = hypothesis("/runtime/health/readiness", reason="readiness 경로 확인", action="경로 확인")
    status, result = await diagnose(fail_event(), Llm(answer(hypothesis(path), good)))
    assert status == "completed"
    assert [h["reason"] for h in result["hypotheses"]] == ["readiness 경로 확인"]
    assert result["spec_paths"] == ["/runtime/health/readiness"]
    assert "hunter2" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_all_hypotheses_dropped_falls_back():
    """다 버려지면 지금처럼 무효 — 워커가 기본 결과(ANALYSIS_INVALID)를 쓴다."""
    with pytest.raises(ValueError):
        await diagnose(fail_event(), Llm(answer(hypothesis("/runtime/env/DB_PASSWORD"))))


def test_env_key_paths_never_read_values_in_case_advice():
    """사례 비교는 env 값을 읽지 않는다 — 진단에 env 키 경로가 남아도 related 에 값이 없다."""
    past = dict(case_id="c1", failed_spec=copy.deepcopy(FAIL_SPEC), resolution=None,
                diagnosis={"summary": "FAIL_RATE", "spec_paths": ["/runtime/env/FAIL_RATE"],
                           "hypotheses": [{"actions": ["FAIL_RATE 제거"]}]})
    advice = compare_case(past, copy.deepcopy(FAIL_SPEC))
    assert advice["related"] == [] and "0.3" not in json.dumps(advice, ensure_ascii=False)


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
            assert kwargs == dict(app="app", repository="org/app", target_env="gcp")
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
