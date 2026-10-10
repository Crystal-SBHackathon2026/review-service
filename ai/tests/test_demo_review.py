"""시연 명세의 정책·수정 범위·배포 설정 계약. 외부 Claude·클러스터 호출 없이 검증한다."""
from __future__ import annotations

import copy
import json

import pytest

from review_ai.graph import initial_state, run_graph
from review_ai.judge.fake_llm import FAKES, ScriptedLLM, oracle_review
from review_ai.judge.prompt import build_request
from review_ai.judge.validate import validate_output
from review_ai.overlay.plan import strategy_ops
from review_ai.patching import apply_ops
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import run_static_check
from tests.conftest import load_sample_dict

BUSAN = "23-fix-busan-resource-limits.yaml"
TOKYO = "24-human-tokyo-manual-bluegreen.yaml"


@pytest.mark.parametrize("env,severity", [("aws", "low"), ("gcp", "low"), ("local", "medium")])
def test_resource_policy_is_scoped_to_local(env, severity):
    spec = load_sample_dict(BUSAN)
    spec["target"]["env"] = env
    finding = next(f for f in run_static_check(DeploySpec.model_validate(spec)) if f["rule_id"] == "RUN-005")
    assert (finding["severity"], finding["autofix"]) == (severity, "allowed")


@pytest.mark.parametrize("resources", [None, {}, {"cpu_request": "100m", "memory_request": "32Mi"},
                                     {"cpu_limit": "500m"}, {"memory_limit": "256Mi"},
                                     {"cpu_limit": None, "memory_limit": None}])
@pytest.mark.parametrize("deterministic,strict", [(False, False), (True, False), (False, True), (True, True)])
async def test_busan_fixes_missing_limits_and_preserves_other_fields(resources, deterministic, strict):
    spec = load_sample_dict(BUSAN)
    if resources is None:
        spec["runtime"].pop("resources")
    else:
        spec["runtime"]["resources"] = resources
    before = copy.deepcopy(spec)
    llm = FAKES["oracle"]()
    final = await run_graph(initial_state(spec, review_id="busan"), llm=llm, retriever=FileRetriever(),
                            deterministic_fixes=deterministic, strict_citations=strict)
    assert final["status"] == "pass" and len(final["rounds"]) == 1
    assert final["rounds"][0]["verdict"] == "fix" and final["rounds"][0]["patch_source"] == "llm"
    assert llm.calls == 1  # RUN-005 는 기존 deterministic 허용 목록을 넓히지 않는다.
    fixed = final["deploy_spec"]
    for key, value in (resources or {}).items():
        if value is not None:
            assert fixed["runtime"]["resources"][key] == value
    assert fixed["runtime"]["resources"]["cpu_limit"] == ((resources or {}).get("cpu_limit") or "250m")
    assert fixed["runtime"]["resources"]["memory_limit"] == ((resources or {}).get("memory_limit") or "128Mi")
    fixed_without_resources = copy.deepcopy(fixed)
    fixed_without_resources["runtime"].pop("resources")
    before_without_resources = copy.deepcopy(before)
    before_without_resources["runtime"].pop("resources", None)
    assert fixed_without_resources == before_without_resources and spec == before
    assert apply_ops(spec, final["applied_ops"]) == fixed


@pytest.mark.parametrize("change", ["cpu_request", "existing_limit", "below_request"])
async def test_resource_patch_cannot_change_requests_existing_limits_or_make_invalid_limits(change):
    spec = load_sample_dict(BUSAN)
    if change == "existing_limit":
        spec["runtime"]["resources"]["cpu_limit"] = "500m"
    if change == "below_request":
        spec["runtime"]["resources"]["memory_request"] = "256Mi"
    def bad_patch(request):
        result = oracle_review(request)
        if change != "below_request":
            result["patch"]["ops"].append({"op": "add", "path": "/runtime/resources/cpu_request" if change == "cpu_request"
                                            else "/runtime/resources/cpu_limit", "value_json": '"100m"'})
        return result
    final = await run_graph(initial_state(spec, review_id="unsafe"), llm=ScriptedLLM(bad_patch),
                            retriever=FileRetriever(), strict_citations=True)
    assert final["status"] == "needs_human" and "PATCH_OUT_OF_SCOPE" in final["decision"]["reasons"]
    assert not final["patched"] and final["deploy_spec"] == spec


@pytest.mark.parametrize("flag", ["autofix_commit", "generated_spec", "human_decision"])
async def test_busan_does_not_bypass_loop_or_human_guards(flag):
    state = initial_state(load_sample_dict(BUSAN), review_id="guard")
    state[flag] = {"decision": "approved", "approver": "local", "edited_ops": [], "use_recommendations": False} if flag == "human_decision" else True
    final = await run_graph(state, llm=FAKES["oracle"](), retriever=FileRetriever(),
                            deterministic_fixes=True, strict_citations=True)
    assert final["status"] == "needs_human" and not final["patched"]
    expected = "GENERATED_SPEC_UNVERIFIED" if flag == "generated_spec" else "LOOP_EXHAUSTED"
    assert expected in final["decision"]["reasons"]


@pytest.mark.parametrize("rollout", [None, {}, {"strategy": "canary"}, {"auto_promotion": False},
                                   {"strategy": "canary", "auto_promotion": False}])
async def test_bluegreen_patch_handles_missing_parent_and_preserves_promotion_setting(rollout):
    spec = load_sample_dict("04-human-engine-change-with-data.yaml")
    spec["baseline"]["facts"]["database_has_data"] = False
    if rollout is None:
        spec.pop("rollout", None)
    else:
        spec["rollout"] = rollout
    findings = run_static_check(DeploySpec.model_validate(spec))
    docs = await FileRetriever().search(findings, spec["target"]["env"])
    request = build_request(spec, findings, docs, spec["target"]["env"])
    result = oracle_review(request)
    _, validation, patch = validate_output(json.dumps(result), findings, docs, spec, strict_citations=True)
    assert validation["patch_scope_ok"] and patch is not None
    fixed = apply_ops(spec, patch["ops"])
    assert fixed["rollout"]["strategy"] == "bluegreen"
    if rollout and "auto_promotion" in rollout:
        assert fixed["rollout"]["auto_promotion"] is False
    if rollout is None:
        result["patch"]["ops"] = [{"op": "add", "path": "/rollout/strategy", "value_json": '"bluegreen"'}]
        _, invalid, rejected = validate_output(json.dumps(result), findings, docs, spec, strict_citations=True)
        assert not invalid["patch_scope_ok"] and rejected is None


@pytest.mark.parametrize("deterministic,strict", [(False, False), (True, False), (False, True), (True, True)])
async def test_tokyo_requires_human_and_renders_manual_promotion(deterministic, strict):
    spec = load_sample_dict(TOKYO)
    final = await run_graph(initial_state(spec, review_id="tokyo"), llm=FAKES["oracle"](),
                            retriever=FileRetriever(), deterministic_fixes=deterministic, strict_citations=strict)
    assert final["status"] == "needs_human" and "AUTOFIX_FORBIDDEN" in final["decision"]["reasons"]
    assert not final["patched"]
    assert strategy_ops(DeploySpec.model_validate(final["deploy_spec"]))[0]["value"]["blueGreen"]["autoPromotionEnabled"] is False


def test_existing_bluegreen_defaults_keep_auto_promotion():
    spec = load_sample_dict(TOKYO)
    spec["rollout"].pop("auto_promotion")
    assert strategy_ops(DeploySpec.model_validate(spec))[0]["value"]["blueGreen"]["autoPromotionEnabled"] is True
