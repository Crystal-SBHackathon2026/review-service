"""미입력 권장값의 생성·우선순위·재검사와 원본 커밋에 필요한 기록."""

from __future__ import annotations

import copy

import pytest

from review_ai.graph import initial_state, route_start, run_graph
from review_ai.judge.fake_llm import FAKES
from review_ai.patching import PatchError, apply_ops
from review_ai.recommendations import build_recommendations, resolve_human_decision
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import run_static_check
from review_ai.retrieval.file_retriever import FileRetriever
from tests.conftest import load_sample_dict


async def pause(name: str, *, llm: str = "oracle") -> dict:
    return await run_graph(initial_state(load_sample_dict(name), review_id="defaults"),
                           llm=FAKES[llm](), retriever=FileRetriever())


async def resume(state: dict, *ops: dict, use_recommendations: bool = True) -> dict:
    return await run_graph({**state, "human_decision": {"decision": "approved", "approver": "tester",
                           "edited_ops": list(ops), "use_recommendations": use_recommendations}},
                           llm=FAKES["oracle"](), retriever=FileRetriever())


async def test_no_values_restores_previous_database_and_records_defaults() -> None:
    state = await pause("04-human-engine-change-with-data.yaml")
    assert state["status"] == "needs_human"  # 제안만으로 적용되지 않는다
    assert state["deploy_spec"]["database"]["engine"] == "mysql"
    assert state["decision"]["recommendations"][0]["source"] == "baseline"
    final = await resume(state)
    assert final["status"] == "pass"
    assert final["deploy_spec"]["database"]["engine"] == "postgres"
    assert final["deploy_spec"]["database"]["version"] == "16"
    assert final["rounds"][0]["defaulted_ops"] == final["applied_ops"]
    original = load_sample_dict("04-human-engine-change-with-data.yaml")
    assert apply_ops(original, final["applied_ops"]) == final["deploy_spec"]
    assert original["database"]["engine"] == "mysql"


async def test_partial_answer_keeps_user_value_and_fills_version() -> None:
    state = await pause("04-human-engine-change-with-data.yaml")
    choice = {"op": "replace", "path": "/database/engine", "value": "postgres"}
    final = await resume(state, choice)
    assert final["status"] == "pass"
    assert final["applied_ops"][-1] == choice
    assert final["rounds"][0]["defaulted_ops"] == [{"op": "add", "path": "/database/version", "value": "16"}]


@pytest.mark.parametrize("value", [None, "", "   "])
async def test_unanswered_volume_uses_previous_size(value: str | None) -> None:
    state = await pause("08-human-volume-shrink-local.yaml")
    op = {"op": "replace", "path": "/storage/volumes/0/size"}
    if value is not None:  # value 키 자체의 누락도 미입력이다
        op["value"] = value
    final = await resume(state, op)
    assert final["status"] == "pass"
    assert final["deploy_spec"]["storage"]["volumes"][0]["size"] == "10Gi"


async def test_explicit_value_overrides_recommendation() -> None:
    state = await pause("08-human-volume-shrink-local.yaml")
    choice = {"op": "replace", "path": "/storage/volumes/0/size", "value": "20Gi"}
    final = await resume(state, choice)
    assert final["status"] == "pass"
    assert final["applied_ops"] == [choice]
    assert "defaulted_ops" not in final["rounds"][0]


async def test_explicit_unsafe_choice_is_rechecked_not_replaced() -> None:
    state = await pause("08-human-volume-shrink-local.yaml")
    final = await resume(state, {"op": "replace", "path": "/storage/volumes/0/size", "value": "1Gi"})
    assert final["status"] == "needs_human"
    assert final["deploy_spec"]["storage"]["volumes"][0]["size"] == "1Gi"


async def test_unknown_probe_is_not_invented_and_remaining_problem_stays_human() -> None:
    state = await pause("10-human-mixed-aws.yaml")
    final = await resume(state)
    assert final["status"] == "needs_human"
    assert [f["rule_id"] for f in final["findings"]] == ["RUN-001"]
    assert not final["deploy_spec"]["storage"]["buckets"][0]["public"]
    assert not final["deploy_spec"]["runtime"]["health"].get("readiness")


async def test_no_recommendation_and_no_values_is_plain_approval() -> None:
    """권장값을 못 만드는 사유(평문 비밀 등)에서 값 없는 승인은 기존 계약대로 그대로 승인이다."""
    state = await pause("06-human-plaintext-secret.yaml")
    assert not state["decision"]["recommendations"]
    human = {"decision": "approved", "approver": "tester", "edited_ops": []}

    resolved = resolve_human_decision(state, human)

    assert resolved["edited_ops"] == [] and resolved["defaulted_ops"] == []
    assert route_start({**state, "human_decision": human}) == "static_check"


async def test_same_path_with_and_without_value_is_rejected() -> None:
    state = await pause("04-human-engine-change-with-data.yaml")
    ops = [{"op": "replace", "path": "/database/version", "value": "15"}, {"op": "replace", "path": "/database/version"}]
    with pytest.raises(PatchError, match="함께"):
        resolve_human_decision(state, {"decision": "approved", "approver": "tester", "edited_ops": ops})


async def test_unanswered_path_without_recommendation_is_rejected() -> None:
    state = await pause("10-human-mixed-aws.yaml")
    with pytest.raises(PatchError, match="권장값이 없다"):
        await resume(state, {"op": "add", "path": "/runtime/health/readiness"})


@pytest.mark.parametrize("path", [
    "/storage/volumes/0/size/typo",
    "/storage/volumes/0/unknown",
    "/storage/volumes/1/size",
    "/storage/volumes/-/size",
    "/storage/volumes/00/size",
])
@pytest.mark.parametrize("value", [None, "   "])
async def test_unanswered_invalid_path_is_rejected_before_defaulting(path: str, value: str | None) -> None:
    state = await pause("08-human-volume-shrink-local.yaml")
    volume = {**state["deploy_spec"]["storage"]["volumes"][0], "size": "10Gi"}
    # 상위 객체 권장값이 있어도 임의의 하위 키·인덱스를 허용하면 안 된다.
    state["decision"]["recommendations"][0]["ops"] = [
        {"op": "replace", "path": "/storage/volumes", "value": [volume]}]
    original = copy.deepcopy(state)
    op = {"op": "replace", "path": path}
    if value is not None:
        op["value"] = value
    with pytest.raises(PatchError, match="경로"):
        resolve_human_decision(state, {"decision": "approved", "approver": "tester", "edited_ops": [op]})
    assert state == original


@pytest.mark.parametrize("path", ["/storage/volumes/0", "/storage/volumes", "/storage"])
async def test_unanswered_parent_of_recommended_field_is_allowed(path: str) -> None:
    state = await pause("08-human-volume-shrink-local.yaml")
    final = await resume(state, {"op": "replace", "path": path})
    assert final["status"] == "pass"
    assert final["deploy_spec"]["storage"]["volumes"][0]["size"] == "10Gi"


async def test_unanswered_child_of_recommended_object_is_allowed() -> None:
    state = await pause("08-human-volume-shrink-local.yaml")
    volume = {**state["deploy_spec"]["storage"]["volumes"][0], "size": "10Gi"}
    state["decision"]["recommendations"][0]["ops"] = [
        {"op": "replace", "path": "/storage/volumes/0", "value": volume}]
    final = await resume(state, {"op": "replace", "path": "/storage/volumes/0/size"})
    assert final["status"] == "pass"
    assert final["deploy_spec"]["storage"]["volumes"][0]["size"] == "10Gi"


async def test_unanswered_optional_field_added_by_recommendation_is_allowed() -> None:
    state = await pause("04-human-engine-change-with-data.yaml")
    state["deploy_spec"]["database"].pop("version")
    final = await resume(state, {"op": "add", "path": "/database/version"})
    assert final["status"] == "pass"
    assert final["deploy_spec"]["database"]["version"] == "16"


async def test_rejected_decision_does_not_apply_defaults() -> None:
    state = await pause("04-human-engine-change-with-data.yaml")
    human = {"decision": "rejected", "approver": "tester", "edited_ops": []}
    assert resolve_human_decision(state, human) == human


async def test_baseline_default_does_not_need_llm_or_guess_secrets() -> None:
    state = await pause("04-human-engine-change-with-data.yaml", llm="unavailable")
    assert state["decision"]["recommendations"]
    assert all(op["path"].startswith("/database/") for rec in state["decision"]["recommendations"] for op in rec["ops"])


@pytest.mark.parametrize("op", [
    {"op": "replace", "path": "/storage/buckets/0/public", "value": False},
    {"op": "replace", "path": "/database/version", "value": None},
    {"op": "add", "path": "/database/backup_retention_days", "value": 0},
])
async def test_false_zero_and_null_are_explicit_values(op: dict) -> None:
    state = await pause("10-human-mixed-aws.yaml" if op["path"].startswith("/storage/") else "04-human-engine-change-with-data.yaml")
    resolved = resolve_human_decision(state, {"decision": "approved", "approver": "tester", "edited_ops": [op]})
    assert resolved["edited_ops"][-1] == op
    assert all(default["path"] != op["path"] for default in resolved["defaulted_ops"])


async def test_parent_object_choice_is_not_overwritten() -> None:
    state = await pause("08-human-volume-shrink-local.yaml")
    volume = {**state["deploy_spec"]["storage"]["volumes"][0], "size": "20Gi"}
    op = {"op": "replace", "path": "/storage/volumes/0", "value": volume}
    final = await resume(state, op)
    assert final["applied_ops"] == [op]


async def test_user_child_field_overrides_recommended_object() -> None:
    state = await pause("08-human-volume-shrink-local.yaml")
    volume = {**state["deploy_spec"]["storage"]["volumes"][0], "size": "10Gi"}
    state["decision"]["recommendations"][0]["ops"] = [{"op": "replace", "path": "/storage/volumes/0", "value": volume}]
    choice = {"op": "replace", "path": "/storage/volumes/0/size", "value": "20Gi"}
    final = await resume(state, choice)
    assert final["status"] == "pass"
    assert final["deploy_spec"]["storage"]["volumes"][0]["size"] == "20Gi"
    assert final["applied_ops"][-1] == choice


@pytest.mark.parametrize("mutation", ["different_app", "unsupported_version"])
async def test_unusable_baseline_does_not_produce_defaults(mutation: str) -> None:
    spec = load_sample_dict("04-human-engine-change-with-data.yaml")
    if mutation == "different_app":
        spec["baseline"]["spec"]["metadata"]["name"] = "different-app"
        with pytest.raises(ValueError, match="baseline"):
            await run_graph(initial_state(spec, review_id="bad-baseline"),
                            llm=FAKES["oracle"](), retriever=FileRetriever())
        return
    else:
        spec["baseline"]["spec"]["database"]["version"] = "1"  # 적용 시 DB-002가 새로 생긴다
    state = await run_graph(initial_state(spec, review_id="bad-baseline"),
                            llm=FAKES["oracle"](), retriever=FileRetriever())
    assert state["status"] == "needs_human"
    assert not state["decision"]["recommendations"]


async def test_explicit_opt_out_preserves_previous_approval_behavior() -> None:
    state = await pause("04-human-engine-change-with-data.yaml")
    original = copy.deepcopy(state)
    resolved = resolve_human_decision(state, {"decision": "approved", "approver": "tester", "edited_ops": [],
                                            "use_recommendations": False})
    assert not resolved["edited_ops"]
    assert state == original


# ── P1 규칙 권장값: 규칙·baseline 으로 확정할 수 있는 값만 ──────────────────


def recommend(spec: dict) -> list[dict]:
    return build_recommendations(spec, run_static_check(DeploySpec.model_validate(spec)))


def test_rule_recommendations_for_p1_rules() -> None:
    spec = load_sample_dict("01-pass-sample-app-aws.yaml")
    spec["database"] = {"engine": "postgres", "version": "16", "placement": "managed", "publicly_accessible": True}
    spec["secrets"] = [{"name": "DATABASE_URL", "source": "aws-secrets-manager", "key": "app/db"}]
    spec["runtime"]["env"]["DATABASE_URL"] = "postgres://db:5432/app"
    del spec["runtime"]["resources"]

    ops = {op["path"]: op for rec in recommend(spec) for op in rec["ops"]}

    assert ops["/database/publicly_accessible"]["value"] is False
    assert ops["/runtime/env/DATABASE_URL"] == {"op": "remove", "path": "/runtime/env/DATABASE_URL"}
    assert ops["/runtime/resources"]["value"] == {"cpu_limit": "250m", "memory_limit": "128Mi"}  # 원문에 resources 가 없다


def test_removed_volumes_are_restored_from_baseline_in_one_candidate() -> None:
    spec = load_sample_dict("02-pass-local-sqlite.yaml")
    uploads = {"name": "uploads", "mount_path": "/uploads", "size": "2Gi", "persistent": True, "access_mode": "ReadWriteOnce"}
    cache = {**uploads, "name": "cache", "mount_path": "/cache"}
    prev = copy.deepcopy(spec)
    prev["storage"]["volumes"] += [uploads, cache]
    spec["baseline"] = {"spec_ref": "prev", "spec": prev}

    [rec] = recommend(spec)

    assert (rec["source"], len(rec["finding_ids"])) == ("baseline", 2)
    restored = apply_ops(spec, rec["ops"])["storage"]["volumes"]
    assert [v["name"] for v in restored] == ["data", "uploads", "cache"]


async def test_sto006_unanswered_approval_restores_the_volume() -> None:
    spec = load_sample_dict("02-pass-local-sqlite.yaml")
    prev = copy.deepcopy(spec)
    prev["storage"]["volumes"].append({"name": "uploads", "mount_path": "/uploads", "size": "2Gi"})
    spec["baseline"] = {"spec_ref": "prev", "spec": prev}
    state = await run_graph(initial_state(spec, review_id="sto006"), llm=FAKES["oracle"](), retriever=FileRetriever())
    assert (state["status"], state["decision"]["reasons"]) == ("needs_human", ["IRREVERSIBLE"])

    final = await resume(state)

    assert final["status"] == "pass"
    assert [v["name"] for v in final["deploy_spec"]["storage"]["volumes"]] == ["data", "uploads"]
