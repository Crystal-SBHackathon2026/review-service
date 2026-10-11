import pytest

from review_ai.graph import check_edited_ops
from review_ai.recommendations import build_recommendations, resolve_human_decision
from review_ai.resource_limits import check_resource_limits
from review_ai.spec.deploy_spec import DeploySpec, Resources
from review_ai.static_check import run_static_check
from tests.conftest import load_sample_dict


@pytest.mark.parametrize("requests,limits,valid", [
    ({"cpu_request": "0.5"}, {"cpu_limit": "500m"}, True),
    ({"cpu_request": "501m"}, {"cpu_limit": "0.5"}, False),
    ({"cpu_request": "2"}, {"cpu_limit": "2000m"}, True),
    ({"memory_request": "1Gi"}, {"memory_limit": "1024Mi"}, True),
    ({"memory_request": "1025Mi"}, {"memory_limit": "1Gi"}, False),
    ({"memory_request": "1Ti"}, {"memory_limit": "1024Gi"}, True),
    ({"cpu_request": "1", "memory_request": "1Gi"}, {}, True),
])
def test_resource_comparison_accepts_equal_quantities_and_missing_limits(requests, limits, valid):
    resources = Resources(**requests, **limits)
    if valid:
        check_resource_limits(resources)
    else:
        with pytest.raises(ValueError, match="이상이어야"):
            check_resource_limits(resources)


@pytest.mark.parametrize("request_field,value", [("cpu_request", "500m"), ("memory_request", "256Mi")])
def test_unsafe_resource_defaults_are_not_recommended_or_implicitly_approved(request_field, value):
    spec = load_sample_dict("23-fix-busan-resource-limits.yaml")
    spec["runtime"]["resources"][request_field] = value
    recommendations = build_recommendations(spec, run_static_check(DeploySpec.model_validate(spec)))
    assert not recommendations
    state = {"deploy_spec": spec, "decision": {"recommendations": recommendations}}
    human = {"decision": "approved", "approver": "tester"}
    with pytest.raises(ValueError, match="안전한 기본 리소스 상한이 없다"):
        resolve_human_decision(state, human)
    # 상한 생략을 명시적으로 수용하는 기존 계약은 유지한다.
    assert not resolve_human_decision(state, {**human, "use_recommendations": False})["edited_ops"]


@pytest.mark.parametrize("use_recommendations", [False, True])
def test_human_request_edit_cannot_make_a_previously_valid_limit_invalid(use_recommendations):
    spec = load_sample_dict("24-human-tokyo-manual-bluegreen.yaml")
    op = {"op": "add", "path": "/runtime/resources/memory_request", "value": "1Ti"}
    with pytest.raises(ValueError, match="memory_limit"):
        check_edited_ops(spec, [op])
    with pytest.raises(ValueError, match="memory_limit"):
        resolve_human_decision({"deploy_spec": spec}, {
            "decision": "approved", "approver": "tester", "edited_ops": [op],
            "use_recommendations": use_recommendations,
        })


def test_old_stored_recommendations_are_checked_again_on_approval():
    spec = load_sample_dict("23-fix-busan-resource-limits.yaml")
    spec["runtime"]["resources"]["memory_request"] = "256Mi"
    state = {"deploy_spec": spec, "decision": {"recommendations": [{"ops": [
        {"op": "add", "path": "/runtime/resources/memory_limit", "value": "128Mi"},
    ]}]}}
    with pytest.raises(ValueError, match="memory_limit"):
        resolve_human_decision(state, {"decision": "approved", "approver": "tester"})
    # 오래된 권장값을 안전한 사용자 값으로 덮어쓰면 승인할 수 있다.
    resolved = resolve_human_decision(state, {"decision": "approved", "approver": "tester", "edited_ops": [
        {"op": "add", "path": "/runtime/resources/memory_limit", "value": "256Mi"},
    ]})
    assert resolved["edited_ops"] == [{"op": "add", "path": "/runtime/resources/memory_limit", "value": "256Mi"}]
