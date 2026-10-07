from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from review_ai.masking import mask_spec
from review_ai.messages import ReviewRequested, build_review_requested, spec_sha256
from review_ai.secrets_pattern import MASK
from tests.conftest import load_sample_dict

KST = timezone(timedelta(hours=9))
SPEC_REF = {"repository": "Crystal-SBHackathon2026/sample-app", "commit": "c7e5d10", "path": "deploy.yaml"}


def test_mask_spec_hides_secret_env_and_keeps_the_rest() -> None:
    spec = load_sample_dict("06-human-plaintext-secret.yaml")
    masked = mask_spec(spec)
    assert masked["runtime"]["env"] == {"DEPLOY_ENV": "aws", "EXTERNAL_API_TOKEN": MASK}
    assert "sample-not-a-real-token-0000" not in repr(masked)
    assert masked["image"] == spec["image"]


def test_mask_spec_does_not_mutate_input() -> None:
    spec = load_sample_dict("06-human-plaintext-secret.yaml")
    before = copy.deepcopy(spec)
    mask_spec(spec)
    assert spec == before


def test_mask_spec_masks_credential_urls_anywhere_including_baseline() -> None:
    spec = load_sample_dict("04-human-engine-change-with-data.yaml")
    spec["runtime"]["env"] = {"CACHE_URL": "redis://app:hunter2@cache:6379"}
    spec["baseline"]["spec"]["runtime"]["env"] = {"OLD_DSN": "postgres://u:p@db/x"}
    masked = mask_spec(spec)
    assert masked["runtime"]["env"]["CACHE_URL"] == MASK
    assert masked["baseline"]["spec"]["runtime"]["env"]["OLD_DSN"] == MASK


def test_mask_spec_is_idempotent() -> None:
    spec = load_sample_dict("06-human-plaintext-secret.yaml")
    assert mask_spec(mask_spec(spec)) == mask_spec(spec)


def _message(spec: dict[str, Any]) -> ReviewRequested:
    return build_review_requested(
        spec,
        review_id="rv_20261007_0001",
        spec_ref=SPEC_REF,
        requested_by="github:yeonjae1220",
        requested_at=datetime(2026, 10, 7, 18, 30, tzinfo=KST),
    )


def test_build_message_strips_baseline_and_masks() -> None:
    msg = _message(load_sample_dict("04-human-engine-change-with-data.yaml"))
    assert "baseline" not in msg.deploy_spec
    assert msg.repo_id == "Crystal-SBHackathon2026/sample-orders"
    assert (msg.app, msg.target_env, msg.schema_version) == ("orders", "aws", "review.requested/v1")
    assert msg.spec_sha256 == spec_sha256(msg.deploy_spec)


def test_message_has_no_plaintext_secret_and_stays_small() -> None:
    body = _message(load_sample_dict("06-human-plaintext-secret.yaml")).model_dump_json()
    assert "sample-not-a-real-token-0000" not in body
    assert len(body.encode()) < 2048


def test_hash_is_order_independent() -> None:
    spec = load_sample_dict("01-pass-sample-app-aws.yaml")
    reordered = dict(reversed(list(spec.items())))
    assert spec_sha256(spec) == spec_sha256(reordered)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["deploy_spec"]["runtime"]["env"].update(EXTERNAL_API_TOKEN="leak"),
        lambda d: d["deploy_spec"].update(baseline={"spec_ref": "x"}),
        lambda d: d.update(spec_sha256="0" * 64),
    ],
    ids=["unmasked-secret", "baseline-included", "hash-mismatch"],
)
def test_message_model_rejects_unsafe_payloads(mutate: Any) -> None:
    data = _message(load_sample_dict("06-human-plaintext-secret.yaml")).model_dump(mode="json")
    mutate(data)
    with pytest.raises(ValidationError):
        ReviewRequested.model_validate(data)


# ── 리뷰 지적 회귀: 비밀 판별 범위 ───────────────────────────────

from review_ai.secrets_pattern import is_secret_name, looks_secret  # noqa: E402


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DB_PASS", "x"), ("DB_PWD", "x"), ("STRIPE_KEY", "x"), ("JWT_SIGNING_KEY", "x"),
        ("AUTH_HEADER", "Bearer abc.def"), ("AWS_ID", "AKIAABCDEFGHIJKLMNOP"),
        ("GH", "ghp_abcdefghijklmnopqrstuvwxyz0123"), ("KEY_PEM", "-----BEGIN RSA PRIVATE KEY-----\nx"),
        ("REDIS_URL", "redis://:pw@h:6379"), ("PG_URL", "postgres://u:ab/cd@h/db"),
        ("JDBC_URL", "jdbc:mysql://h/db?user=u&password=x"), ("UPSTREAM", "user:pw@host"),
        ("ANY", MASK),
    ],
)
def test_looks_secret_catches_common_forms(name: str, value: str) -> None:
    assert looks_secret(name, value)


@pytest.mark.parametrize("name", ["BYPASS_CACHE", "PASSPORT_REGION", "AUTH_ENABLED", "SESSION_TIMEOUT", "LOG_LEVEL"])
def test_secret_name_avoids_common_false_positives(name: str) -> None:
    assert not is_secret_name(name)


def test_masked_spec_still_triggers_sec001() -> None:
    from review_ai.spec.deploy_spec import DeploySpec
    from review_ai.static_check import run_static_check

    spec = load_sample_dict("01-pass-sample-app-aws.yaml")
    spec["runtime"]["env"]["DATABASE_URL"] = "postgres://u:pw@h/db"
    for candidate in (spec, mask_spec(spec)):
        rules = [f["rule_id"] for f in run_static_check(DeploySpec.model_validate(candidate))]
        assert "SEC-001" in rules


def test_non_string_secret_values_are_masked_and_not_echoed() -> None:
    spec = load_sample_dict("01-pass-sample-app-aws.yaml")
    spec["runtime"]["env"]["DB_PASSWORD"] = 97531864
    assert mask_spec(spec)["runtime"]["env"]["DB_PASSWORD"] == MASK
    assert "97531864" not in _message(spec).model_dump_json()  # 가린 뒤 검증하므로 에러에도 값이 남지 않는다


def test_secret_values_in_free_text_fields_are_masked() -> None:
    spec = load_sample_dict("01-pass-sample-app-aws.yaml")
    spec["image"]["tag"] = "ghp_abcdefghijklmnopqrstuvwxyz0123"
    assert mask_spec(spec)["image"]["tag"] == MASK
