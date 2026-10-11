import pytest
import yaml

from review_ai.overlay.plan import strategy_ops
from review_ai.spec.deploy_spec import DeploySpec
from tests.conftest import FIX_SHA, HEAD, Harness, load_sample, suite


@pytest.mark.parametrize("deterministic,strict", [(False, False), (True, False), (False, True), (True, True)])
async def test_busan_resource_fix_commits_once_and_verifies_new_head(deterministic, strict):
    h = Harness(deterministic_fixes=deterministic, strict_citations=strict)
    spec = load_sample("23-fix-busan-resource-limits.yaml")
    await h.request(spec)
    parent = h.row()
    assert parent["status"] == "superseded" and parent["rounds"][0]["verdict"] == "fix"
    child_id = parent["superseded_by"]
    fixed = yaml.safe_load(h.github.commits[0]["content"])
    assert fixed["runtime"]["resources"] == {**spec["runtime"]["resources"], "cpu_limit": "250m", "memory_limit": "128Mi"}
    await h.deliver_published()
    child = h.row(child_id)
    assert (child["status"], child["verdict"], child["pr_head_sha"]) == ("waiting_ci", "pass", FIX_SHA)
    assert len(h.github.commits) == 1 and not h.github.merged
    h.github.suites[FIX_SHA] = [suite()]
    await h.ci(child_id, head_sha=FIX_SHA)
    assert h.github.merged == [(spec["metadata"]["repository"], 7, FIX_SHA)]


@pytest.mark.parametrize("deterministic,strict", [(False, False), (True, False), (False, True), (True, True)])
async def test_tokyo_approval_resumes_without_edit_or_bot_commit(deterministic, strict):
    h = Harness(deterministic_fixes=deterministic, strict_citations=strict)
    spec = load_sample("24-human-tokyo-manual-bluegreen.yaml")
    await h.request(spec)
    assert h.row()["status"] == "needs_human" and not h.github.merged
    assert "AUTOFIX_FORBIDDEN" in h.row()["reasons"]
    await h.human("rv_20261008_test", "approved")
    assert h.row()["status"] == "waiting_ci" and not h.github.commits
    h.github.suites[HEAD] = [suite()]
    await h.ci("rv_20261008_test")
    assert h.github.merged == [(spec["metadata"]["repository"], 7, HEAD)]
    assert strategy_ops(DeploySpec.model_validate(h.row()["final_spec"]))[0]["value"]["blueGreen"]["autoPromotionEnabled"] is False


async def test_tokyo_canary_recommendation_does_not_silently_approve_breaking_change():
    """전략 수정 뒤 breaking 사유가 남으면 다시 사람 확인한다. 단일 승인 시연은 BG 원안을 사용한다."""
    h = Harness(strict_citations=True)
    spec = load_sample("24-human-tokyo-manual-bluegreen.yaml")
    spec["rollout"]["strategy"] = "canary"
    await h.request(spec)
    await h.human("rv_20261008_test", "approved")
    assert h.row()["status"] == "needs_human" and h.row()["final_spec"]["rollout"]["strategy"] == "bluegreen"
    assert not h.github.commits and not h.github.merged


@pytest.mark.parametrize("request_field,request_value,limit_field,safe_limit", [
    ("cpu_request", "0.5", "cpu_limit", "500m"),
    ("memory_request", "1Gi", "memory_limit", "1024Mi"),
])
async def test_busan_large_request_requires_safe_human_limit_before_commit(
    request_field, request_value, limit_field, safe_limit,
):
    h = Harness(strict_citations=True)
    spec = load_sample("23-fix-busan-resource-limits.yaml")
    spec["runtime"]["resources"][request_field] = request_value
    await h.request(spec)
    assert h.row()["status"] == "needs_human"
    assert not h.row()["decision"]["recommendations"]
    await h.human("rv_20261008_test", "approved")
    assert h.row()["status"] == "needs_human" and h.row()["error"]
    assert not h.github.prepared and not h.github.commits and not h.github.merged

    # 두 상한을 명시한다. 안전한 입력만 새 SHA 재검토로 진행한다.
    limits = {"cpu_limit": "250m", "memory_limit": "128Mi", limit_field: safe_limit}
    await h.human("rv_20261008_test", "approved", [
        {"op": "add", "path": f"/runtime/resources/{field}", "value": value}
        for field, value in limits.items()
    ])
    assert h.row()["status"] == "superseded" and len(h.github.commits) == 1
    fixed = yaml.safe_load(h.github.commits[0]["content"])
    assert fixed["runtime"]["resources"] == {**spec["runtime"]["resources"], **limits}
    child_id = h.row()["superseded_by"]
    await h.deliver_published()
    assert (h.row(child_id)["status"], h.row(child_id)["verdict"], h.row(child_id)["pr_head_sha"]) == (
        "waiting_ci", "pass", FIX_SHA,
    )


@pytest.mark.parametrize("whole_object", [False, True])
async def test_busan_invalid_human_limit_returns_to_approval_without_preparing_commit(whole_object):
    h = Harness(strict_citations=True)
    spec = load_sample("23-fix-busan-resource-limits.yaml")
    spec["runtime"]["resources"]["memory_request"] = "256Mi"
    await h.request(spec)
    ops = [{"op": "add", "path": "/runtime/resources/memory_limit", "value": "128Mi"}]
    if whole_object:
        ops = [{"op": "add", "path": "/runtime/resources", "value": {
            **spec["runtime"]["resources"], "cpu_limit": "250m", "memory_limit": "128Mi",
        }}]
    await h.human("rv_20261008_test", "approved", ops)
    assert h.row()["status"] == "needs_human" and h.row()["error"]
    assert not h.github.prepared and not h.github.commits and not h.github.merged
