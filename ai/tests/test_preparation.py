"""빈 명세 생성과 기존 리뷰 연결. 자동값이 확인된 사실로 둔갑하지 않게 한다."""

from __future__ import annotations

import copy
import json

import pytest
import yaml
from pydantic import ValidationError

from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.preparation import GenerationContext, prepare_and_review, prepare_spec
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.spec.deploy_spec import AppSpec, Baseline


def context_for(spec: dict, *, env: str | None = None) -> GenerationContext:
    target = {**spec["target"], **({"env": env} if env else {})}
    return GenerationContext(repository=spec["metadata"]["repository"], target=target)


def verified_context(spec: dict) -> GenerationContext:
    app = AppSpec.model_validate(spec)
    return GenerationContext(repository=app.metadata.repository, name=app.metadata.name, target=app.target,
                             **{field: getattr(app, field) for field in
                                ("image", "runtime", "database", "requirements", "secrets", "storage", "network")})


@pytest.mark.parametrize("raw", [None, "", " \n\t ", "# no spec\n", "---\n", "null", {}, "{}"])
def test_only_empty_input_generates(raw: str | dict | None, sample_app: dict) -> None:
    result = prepare_spec(raw, context=context_for(sample_app))

    assert result.origin == "generated"
    assert result.spec.runtime.port == 8080
    assert result.spec.runtime.replicas == 1
    assert result.spec.runtime.health.readiness is None
    assert result.spec.database.engine == "none"
    assert result.spec.network.ingress is None
    assert result.spec.image.platforms == ("amd64",)
    assert result.spec.metadata.commit is None
    assert len(result.verification) == 6
    response = result.to_response()
    assert response["preset"] == "container-http/v1"
    assert AppSpec.model_validate(yaml.safe_load(response["yaml"])) == result.spec
    assert {r["source"] for r in response["recommendations"]} == {"catalog", "preset"}


@pytest.mark.parametrize("raw", ["[]", "false", "0", [], False, 0, {"runtime": {"port": 3000}}, "broken: ["])
def test_invalid_nonempty_input_is_not_replaced(raw: object, sample_app: dict) -> None:
    with pytest.raises((ValueError, yaml.YAMLError)):
        prepare_spec(raw, context=context_for(sample_app))  # type: ignore[arg-type]


def test_existing_spec_is_semantically_unchanged(sample_app: dict) -> None:
    before = copy.deepcopy(sample_app)
    result = prepare_spec(sample_app, context=context_for(sample_app))

    assert result.origin == "provided"
    assert result.spec == AppSpec.model_validate(before)
    assert not result.recommendations and not result.verification
    assert sample_app == before
    assert result.to_response()["preset"] is None


def test_yaml_input_is_supported(sample_app: dict) -> None:
    assert prepare_spec(yaml.safe_dump(sample_app), context=context_for(sample_app)).origin == "provided"


@pytest.mark.parametrize("repo", ["Owner/123_APP", "Owner/__", "Owner/" + "A" * 90])
def test_app_names_are_valid_and_deterministic(repo: str) -> None:
    context = GenerationContext(repository=repo, target={"env": "aws", "region": "ap-northeast-2"})
    a, b = prepare_spec(None, context=context), prepare_spec({}, context=context)
    assert a.spec == b.spec
    assert len(a.spec.metadata.name) <= 39


def test_context_and_existing_identity_must_match(sample_app: dict) -> None:
    context = context_for(sample_app).model_copy(update={"repository": "other/app"})
    with pytest.raises(ValueError, match="레포·대상"):
        prepare_spec(sample_app, context=context)


def test_context_rejects_invalid_fields(sample_app: dict) -> None:
    with pytest.raises(ValidationError):
        GenerationContext(repository="bad", target=sample_app["target"])
    with pytest.raises(ValidationError):
        GenerationContext(repository="owner/app", target=sample_app["target"], unexpected=True)


def test_baseline_preserves_data_network_and_secrets(sample_app: dict) -> None:
    sample_app["database"] = {"engine": "sqlite", "placement": "volume", "volume": "data"}
    sample_app["requirements"] = {"persistence": True}
    sample_app["storage"] = {"volumes": [{"name": "data", "mount_path": "/data", "size": "4Gi"}]}
    baseline = Baseline(spec_ref="approved-1", spec=sample_app)
    before = baseline.model_dump()
    result = prepare_spec(None, context=context_for(sample_app), baseline=baseline)

    assert result.spec.database == baseline.spec.database
    assert result.spec.storage == baseline.spec.storage
    assert result.spec.network == baseline.spec.network
    assert result.spec.requirements.persistence
    assert not result.verification
    assert {r.source for r in result.recommendations} == {"baseline"}
    assert baseline.model_dump() == before


def test_baseline_cannot_come_from_another_app(sample_app: dict) -> None:
    baseline = Baseline(spec_ref="approved-1", spec=sample_app)
    for updates in ({"repository": "other/app"}, {"target": baseline.spec.target.model_copy(update={"env": "gcp"})},
                    {"name": "other-app"}):
        with pytest.raises(ValueError, match="baseline"):
            prepare_spec(None, context=context_for(sample_app).model_copy(update=updates), baseline=baseline)


async def test_verified_generation_passes_without_extra_human_approval(sample_app: dict) -> None:
    llm = ScriptedLLM(oracle_review)
    result = await prepare_and_review(None, context=verified_context(sample_app), review_id="generated",
                                     llm=llm, retriever=FileRetriever())

    assert result.prepared.origin == "generated"
    assert result.review["status"] == "pass"
    assert result.ready_to_commit
    assert llm.calls == 0  # low 경고만이면 기존 정책대로 LLM 비용을 쓰지 않는다
    assert result.review["retrieved_docs"]  # NET-001 근거는 검색한다
    assert result.to_response()["ready_to_commit"] is True


async def test_missing_readiness_returns_rag_explanation_and_candidate(sample_app: dict) -> None:
    result = await prepare_and_review("", context=context_for(sample_app), review_id="empty",
                                     llm=ScriptedLLM(oracle_review), retriever=FileRetriever())

    assert result.prepared.spec.runtime.port == 8080
    assert result.review["status"] == "needs_human"
    assert [f["rule_id"] for f in result.review["findings"]] == ["RUN-001"]
    assert result.review["retrieved_docs"]
    assert result.review["decision"]["items"][0]["why"]
    assert not result.ready_to_commit


async def test_unverified_data_defaults_do_not_become_approval(sample_app: dict) -> None:
    # 포트·이미지·probe 가 맞아도 DB/영속성 등의 추정값은 배포 승인 근거가 아니다.
    verified = verified_context(sample_app)
    context = GenerationContext(repository=verified.repository, target=verified.target,
                                image=verified.image, runtime=verified.runtime)
    result = await prepare_and_review(None, context=context, review_id="unknown",
                                     llm=None, retriever=FileRetriever())

    assert result.review["status"] == "pass"
    assert not result.ready_to_commit
    assert "DATABASE_UNVERIFIED" in {v.code for v in result.prepared.verification}


async def test_generation_reuses_rag_autofix_and_returns_final_yaml(sample_app: dict) -> None:
    sample_app["target"]["env"] = "local"
    sample_app["network"] = {}
    sample_app["database"] = {"engine": "sqlite", "placement": "volume", "volume": "data"}
    sample_app["requirements"] = {"persistence": True}
    sample_app["storage"] = {"volumes": [{"name": "data", "mount_path": "/data", "size": "1Gi"}]}
    sample_app["runtime"]["replicas"] = 2
    result = await prepare_and_review({}, context=verified_context(sample_app), review_id="fix",
                                     llm=ScriptedLLM(oracle_review), retriever=FileRetriever())

    assert result.review["status"] == "pass" and result.review["patched"]
    assert result.prepared.spec.runtime.replicas == 1
    assert result.prepared.spec.database.engine == "sqlite"
    assert result.review["rounds"][0]["doc_ids"]
    assert yaml.safe_load(result.to_response()["yaml"])["runtime"]["replicas"] == 1
    assert result.ready_to_commit


async def test_renderer_blocks_unsupported_generated_volume(sample_app: dict) -> None:
    sample_app["storage"] = {"volumes": [{"name": "data", "mount_path": "/data", "size": "1Gi"}]}
    result = await prepare_and_review(None, context=verified_context(sample_app), review_id="volume",
                                     llm=ScriptedLLM(oracle_review), retriever=FileRetriever())

    assert not result.ready_to_commit
    assert any(w["code"] == "VOLUME_UNSUPPORTED" and w["blocking"] for w in result.render_warnings)


async def test_baseline_data_protection_still_applies(sample_app: dict) -> None:
    previous = copy.deepcopy(sample_app)
    previous["database"] = {"engine": "postgres", "placement": "managed", "version": "16"}
    baseline = Baseline(spec_ref="approved", spec=previous)
    context = verified_context(sample_app)  # context 의 DB none 이 승인 postgres 를 덮으려 한다
    result = await prepare_and_review(None, context=context, baseline=baseline, review_id="data",
                                     llm=ScriptedLLM(oracle_review), retriever=FileRetriever())

    assert result.review["status"] == "needs_human"
    assert "DB-001" in {f["rule_id"] for f in result.review["findings"]}
    assert not result.ready_to_commit


async def test_responses_mask_secrets_without_changing_internal_original(sample_app: dict) -> None:
    sample_app["runtime"]["env"]["SESSION_SECRET"] = "do-not-return-this-value"
    result = await prepare_and_review(sample_app, context=context_for(sample_app), review_id="secret",
                                     llm=None, retriever=FileRetriever())

    assert result.prepared.spec.runtime.env["SESSION_SECRET"] == "do-not-return-this-value"
    assert "do-not-return-this-value" not in json.dumps(result.to_response())
    assert not result.ready_to_commit


async def test_masked_spec_is_not_rendered_or_marked_ready(sample_app: dict) -> None:
    sample_app["runtime"]["env"]["SESSION_SECRET"] = "***MASKED***"
    result = await prepare_and_review(sample_app, context=context_for(sample_app), review_id="masked",
                                     llm=None, retriever=FileRetriever())
    assert "ORIGINAL_SPEC_REQUIRED" in {v.code for v in result.prepared.verification}
    assert not result.ready_to_commit


async def test_retrieval_failure_does_not_return_success(sample_app: dict) -> None:
    class BrokenRetriever:
        async def search(self, findings: list, target_env: str) -> list:
            raise TimeoutError("retrieval unavailable")

    with pytest.raises(TimeoutError):
        await prepare_and_review(None, context=context_for(sample_app), review_id="failure",
                                 llm=None, retriever=BrokenRetriever())
