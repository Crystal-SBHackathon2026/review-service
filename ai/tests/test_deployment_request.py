import json

import pytest
import yaml

from review_ai.overlay import render_overlay
from review_ai.spec.deploy_spec import AppSpec
from review_ai.spec.deployment_request import DeploymentRequest, registered_targets
from tests.conftest import load_sample_dict


def selection(**overrides):
    return dict(env="gcp", region="asia-northeast1", cluster="tokyo-gke", path="deploy/gcp.yaml", **overrides)


def manifest(targets):
    return DeploymentRequest.model_validate(dict(apiVersion="oneaction/v1", kind="DeploymentRequest", targets=targets))


def test_single_explicit_gcp_selection_and_immutable_contract():
    request = manifest([selection()])
    assert [t.env for t in request.targets] == ["gcp"]
    with pytest.raises(ValueError):
        request.targets[0].env = "aws"


def test_missing_environment_does_not_become_aws():
    with pytest.raises(ValueError):
        manifest([])
    target = selection()
    target.pop("env")
    with pytest.raises(ValueError):
        manifest([target])


def test_duplicate_environment_and_shared_spec_are_rejected():
    with pytest.raises(ValueError):
        manifest([selection(), selection()])
    with pytest.raises(ValueError):
        manifest([selection(), dict(env="aws", region="ap-northeast-2", cluster="in-cluster", path="deploy/gcp.yaml")])


def test_empty_registry_is_deny_by_default():
    assert registered_targets(None) == registered_targets("") == frozenset()
    assert registered_targets(json.dumps([dict(env="gcp", region="asia-northeast1", cluster="tokyo-gke")])) == frozenset({
        ("gcp", "asia-northeast1", "tokyo-gke")})
    with pytest.raises(ValueError):
        registered_targets('{"env": "gcp"}')


def test_manual_gcp_promotion_delay_survives_renderer():
    raw = load_sample_dict("01-pass-sample-app-aws.yaml")
    raw["target"] = dict(env="gcp", region="asia-northeast1")
    raw["rollout"] = dict(strategy="bluegreen", auto_promotion_seconds=30)
    rendered = render_overlay(AppSpec.model_validate(raw))
    k = yaml.safe_load(rendered.files["kustomization.yaml"])
    ops = yaml.safe_load(k["patches"][0]["patch"])
    strategy = next(p["value"] for p in ops if p["path"] == "/spec/strategy")
    assert strategy["blueGreen"]["autoPromotionSeconds"] == 30


def test_canary_cannot_silently_ignore_bluegreen_delay():
    raw = load_sample_dict("01-pass-sample-app-aws.yaml")
    raw["rollout"] = dict(strategy="canary", auto_promotion_seconds=30)
    with pytest.raises(ValueError):
        AppSpec.model_validate(raw)
