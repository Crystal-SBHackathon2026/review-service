from datetime import UTC, datetime
import copy
import json

import httpx
import pytest

from review_api.app import ApiDeps, create_app
from review_api.argocd import ArgoCdEvent, handle_deploy_event
from review_common.repository import InMemoryReviewRepository


class Publisher:
    def __init__(self):
        self.sent = []
    async def send(self, topic, key, value):
        self.sent.append((topic, key, value))


@pytest.fixture
async def setup():
    repo = InMemoryReviewRepository()
    for rid, rev, path in [("old", "a"*40, "/ready"), ("new", "b"*40, "/wrong")]:
        await repo.insert_review(review_id=rid, app="sample-app", target_env="aws", repo_id="org/app",
            spec_ref={"repository": "org/app", "commit": rev, "path": "deploy.yaml"}, pr_head_sha=rev, requested_by="test")
        await repo.update_review(rid, status="committed", gitops_commit_sha=rev, merge_sha=rev,
            final_spec={"metadata": {"name": "sample-app", "repository": "org/app"}, "target": {"env": "aws"},
                        "runtime": {"health": {"readiness": path}}})
    publisher = Publisher()
    app = create_app(ApiDeps(repo=repo, specs=None, publisher=publisher, api_token="operator", argocd_webhook_token="argo"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield repo, client, publisher


def payload(kind="sync_failed", revision="b"*40):
    return {"schema_version": "deployment.result/v1", "event_type": kind, "app": "sample-app", "env": "aws",
        "health": "Healthy", "sync_status": "Synced", "images": [], "revision": "a"*40,
        "argocd_app": "sample-app-aws", "namespace": "sample-app", "cluster_id": "cluster1",
        "operation": {"phase": "Succeeded" if kind == "deployed" else "Failed", "revision": revision,
            "message": 'objects failed to apply: "resource"\npermission denied',
            "started_at": "2026-10-09T14:00:00+09:00", "finished_at": "2026-10-09T14:01:00+09:00"}}


async def post(client, body):
    return await client.post("/webhooks/argocd", json=body, headers={"Authorization": "Bearer argo"})


async def test_failed_sync_with_healthy_old_service_is_preserved(setup):
    repo, client, publisher = setup
    await post(client, payload("deployed", "a"*40))
    response = await post(client, payload())
    eid = response.json()["event_id"]
    assert response.status_code == 202 and response.json()["review_id"] == "new"
    event = await repo.get_deployment(eid)
    assert event["kind"] == "sync_failed" and event["analysis_status"] == "queued"
    assert event["payload"]["operation"]["revision"] != event["payload"]["revision"]
    assert (await repo.get_baseline("sample-app", "aws"))["merge_sha"] == "a"*40
    assert (await repo.get_review("new"))["status"] == "committed"
    assert await repo.get_failure_case(eid)
    assert publisher.sent[0][0] == "deployment.analysis.requested"
    assert "permission denied" not in publisher.sent[0][2].decode()


async def test_no_images_no_matching_revision_is_retained_unlinked(setup):
    repo, client, _ = setup
    body = payload(revision="c"*40)
    body["images"] = ["ghcr.io/org/app:" + "a"*40]  # old stable must not capture new failure
    r = await post(client, body)
    assert r.json()["review_id"] is None
    stored = await repo.get_deployment(r.json()["event_id"])
    assert stored["spec_snapshot"] is None and stored["analysis_status"] == "queued"
    assert await repo.latest_baseline_for_repository("org/app") is not None


async def test_duplicate_observation_time_does_not_create_new_job(setup):
    repo, client, _ = setup
    body = payload()
    first = (await post(client, body)).json()
    body["observed_at"] = "2026-10-09T15:00:00+09:00"
    second = (await post(client, body)).json()
    assert second["duplicate"] and first["event_id"] == second["event_id"]
    assert len(repo.analysis_jobs) == 1
    for i in range(1, 4):
        body["operation"]["message"] = f"additional evidence {i}"
        await post(client, body)
    assert len(repo.observations) == 4
    assert sum(j["status"] == "skipped" for j in repo.analysis_jobs.values()) == 1


async def test_errors_masked_and_new_details_require_operator_token(setup):
    repo, client, _ = setup
    body = payload()
    body["operation"]["message"] = "token=hidden Bearer anothersecret password=unsafe"
    r = await post(client, body)
    eid = r.json()["event_id"]
    stored = json.dumps((await repo.get_deployment(eid))["payload"])
    assert all(secret not in stored for secret in ("hidden", "anothersecret", "unsafe"))
    assert (await client.get("/deployments")).status_code == 401
    assert (await client.get(f"/deployments/{eid}")).status_code == 401
    assert (await client.get("/reviews/new/case-advice")).status_code == 401
    public = (await client.get("/reviews/new")).json()
    assert public["deployment"]["status"] == "failed" and "payload" not in public
    assert (await client.get(f"/deployments/{eid}", headers={"Authorization": "Bearer operator"})).status_code == 200


async def test_json_validation_limits_and_auth(setup):
    repo, client, _ = setup
    body = payload()
    del body["event_type"]
    assert (await post(client, body)).status_code == 422
    body = payload(); body["operation"]["started_at"] = "2026-10-09T12:00:00"
    assert (await post(client, body)).status_code == 422
    body = payload(); body["operation"]["message"] = "x"*9000
    assert (await post(client, body)).status_code == 422
    assert (await client.post("/webhooks/argocd", content=b"x"*300000,
                             headers={"Authorization": "Bearer argo"})).status_code == 413
    assert (await client.post("/webhooks/argocd", json=payload())).status_code == 401
    assert await repo.list_deployments() == []


async def test_degraded_after_healthy_excludes_failed_baseline_and_fallback(setup):
    repo, client, _ = setup
    await post(client, payload("deployed", "a"*40))
    await post(client, payload("deployed"))
    assert (await repo.get_baseline("sample-app", "aws"))["merge_sha"] == "b"*40
    body = payload("health_degraded")
    body["operation"]["phase"] = "Succeeded"; body["health"] = "Degraded"
    await post(client, body)
    assert (await repo.get_baseline("sample-app", "aws"))["merge_sha"] == "a"*40
    assert (await repo.latest_baseline_for_repository("org/app"))["merge_sha"] == "a"*40
    body = payload("deployed"); body["operation"]["started_at"] = "2026-10-09T16:00:00+09:00"
    await post(client, body)
    assert (await repo.get_baseline("sample-app", "aws"))["merge_sha"] == "a"*40


async def test_verified_resolution_needs_related_new_success_and_changed_config(setup):
    repo, client, _ = setup
    failed = (await post(client, payload())).json()["event_id"]
    await repo.insert_review(review_id="fixed", app="sample-app", target_env="aws", repo_id="org/app",
        spec_ref={"repository": "org/app", "commit": "c"*40}, pr_head_sha="c"*40, requested_by="t")
    spec = copy.deepcopy((await repo.get_review("new"))["final_spec"])
    spec["runtime"]["health"]["readiness"] = "/ready"
    await repo.update_review("fixed", status="committed", gitops_commit_sha="c"*40, merge_sha="c"*40, final_spec=spec)
    body = payload("deployed", "c"*40)
    body["operation"]["started_at"] = "2026-10-09T15:00:00+09:00"
    body["operation"]["finished_at"] = "2026-10-09T15:01:00+09:00"
    success = (await post(client, body)).json()["event_id"]
    data = dict(success_event_id=success, cause="readiness 경로 불일치 확인", actions=["readiness 경로 수정"],
                config_paths=["/runtime/health/readiness"])
    url = f"/failure-cases/{failed}/resolution"
    assert (await client.post(url, json=data)).status_code == 401
    r = await client.post(url, json=data, headers={"Authorization": "Bearer operator"})
    assert r.status_code == 200, r.text
    assert r.json()["resolution"]["conditions"][0]["resolved_value"] == "/ready"
    assert (await client.post(url, json=data, headers={"Authorization": "Bearer operator"})).status_code == 409


async def test_legacy_extension_cannot_treat_failed_operation_as_success(setup):
    repo, client, _ = setup
    body = payload()
    del body["schema_version"]; del body["event_type"]
    body["images"] = ["ghcr.io/org/app:" + "a"*40]
    r = await post(client, body)
    assert r.status_code == 202 and r.json()["recorded"] == "sync_failed"
    assert r.json()["review_id"] == "new"
    assert await repo.get_baseline("sample-app", "aws") is None


async def test_unknown_attempt_metadata_bounds_reanalysis_without_dropping_evidence(setup):
    repo, client, _ = setup
    body = payload(); body["operation"] = None; body["revision"] = None
    for i in range(5):
        body["health_message"] = f"error evidence {i}"
        assert (await post(client, body)).status_code == 202
    assert len(repo.observations) == 5
    assert sum(j["status"] == "skipped" for j in repo.analysis_jobs.values()) == 2


def test_committed_schema_matches_receiver():
    from pathlib import Path
    from review_api.deployment_contract import versioned_schema
    saved = json.loads((Path(__file__).parents[1] / "schema/deployment.result.v1.schema.json").read_text())
    assert saved == versioned_schema()


@pytest.mark.parametrize("phase,health,expected", [("Failed", "Healthy", "sync_failed"),
                                                 ("Succeeded", "Degraded", "health_degraded")])
async def test_inconsistent_deployed_label_cannot_hide_failure(setup, phase, health, expected):
    repo, client, _ = setup
    body = payload("deployed")
    body["operation"]["phase"] = phase
    body["health"] = health
    result = (await post(client, body)).json()
    assert result["recorded"] == expected
    assert (await repo.get_deployment(result["event_id"]))["analysis_status"] == "queued"
    assert await repo.get_baseline("sample-app", "aws") is None


async def test_legacy_retry_restores_observation_after_partial_write(setup):
    repo, _, _ = setup
    body = payload("health_degraded")
    del body["schema_version"]; del body["event_type"]
    body["health"] = "Degraded"; body["operation"]["phase"] = "Succeeded"
    body["images"] = ["ghcr.io/org/app:" + "b"*40]
    event = ArgoCdEvent.model_validate(body)
    record = repo.record_deployment
    async def unavailable(_):
        raise RuntimeError("test observation store unavailable")
    repo.record_deployment = unavailable
    with pytest.raises(RuntimeError):
        await handle_deploy_event(repo, event)
    repo.record_deployment = record
    result = await handle_deploy_event(repo, event)
    assert result["duplicate"]
    assert len(await repo.list_deployments()) == 1
    assert len(repo.analysis_jobs) == 1


async def test_success_retry_restores_baseline_after_partial_write(setup):
    repo, _, _ = setup
    event = ArgoCdEvent.model_validate(payload("deployed"))
    upsert = repo.upsert_baseline
    async def unavailable(**_):
        raise RuntimeError("test baseline store unavailable")
    repo.upsert_baseline = unavailable
    with pytest.raises(RuntimeError):
        await handle_deploy_event(repo, event)
    repo.upsert_baseline = upsert
    result = await handle_deploy_event(repo, event)
    assert result["duplicate"] and result["baseline"] == "updated"
    assert (await repo.get_baseline("sample-app", "aws"))["merge_sha"] == "b"*40


async def test_cross_env_degraded_does_not_poison_aws_baseline_or_cases(setup):
    repo, client, _ = setup
    await post(client, payload("deployed"))
    repo.baselines[("sample-app", "aws")]["database_has_data"] = True
    local = {"app": "sample-app", "env": "local", "health": "Degraded", "images": ["org/app:" + "b"*40]}
    first = await post(client, local)
    assert first.json()["cross_env"]
    assert (await post(client, local)).json()["duplicate"]
    assert not await repo.has_failed_deployment("new")
    assert (await repo.get_baseline("sample-app", "aws"))["database_has_data"] is True
    assert not await repo.get_baseline("sample-app", "local")
    assert await repo.find_failure_cases(app="sample-app", repository="org/app", target_env="aws") == []
    public = (await client.get("/reviews/new")).json()
    assert public["deployment"]["status"] == "healthy"


async def test_cross_env_healthy_never_qualifies_as_target_baseline(setup):
    repo, client, _ = setup
    local = {"app": "sample-app", "env": "local", "health": "Healthy", "images": ["org/app:" + "b"*40]}
    await post(client, local)
    assert await repo.get_baseline("sample-app", "aws") is None
    assert await repo.get_baseline("sample-app", "local") is None


@pytest.mark.parametrize("env", ["local", "gcp"])
async def test_versioned_cross_env_failure_is_recorded_without_foreign_spec(setup, env):
    repo, client, _ = setup
    await post(client, payload("deployed"))
    body = payload(); body["env"] = env
    body["cluster_id"] = "tokyo-gke" if env == "gcp" else "crystal-busan"
    result = (await post(client, body)).json()
    assert result["cross_env"] and result["review_id"] == "new" and not result["linked"]
    evidence = await repo.get_deployment(result["event_id"])
    assert evidence["review_id"] is None and evidence["spec_snapshot"] is None
    assert evidence["target_env"] == env and evidence["analysis_status"] == "queued"
    assert not await repo.has_failed_deployment("new")
    progress = (await client.get("/reviews/new/progress")).json()
    assert next(c for c in progress["envs"] if c["env"] == env)["deploy"]["kind"] == "sync_failed"
    assert (await repo.get_baseline("sample-app", "aws"))["merge_sha"] == "b"*40
    assert await repo.find_failure_cases(app="sample-app", repository="org/app", target_env="aws") == []


async def test_sync_failure_progress_not_hidden_by_secondary_healthy(setup):
    repo, client, _ = setup
    await post(client, {"app": "sample-app", "env": "local", "health": "Healthy", "images": ["org/app:" + "b"*40]})
    await post(client, payload())
    progress = (await client.get("/reviews/new/progress")).json()
    assert next(s for s in progress["steps"] if s["key"] == "deploy")["state"] == "failed"
    assert progress["review"]["deployment"]["status"] == "failed"
    assert "permission denied" not in json.dumps(progress)


async def test_real_gitops38_sample_matches_healthy_image_despite_revision_difference(setup):
    from pathlib import Path
    repo, client, _ = setup
    body = json.loads((Path(__file__).parent / "fixtures/argocd_gitops38_deployed.json").read_text())
    await repo.update_review("new", merge_sha=body["images"][0].split(":")[-1])
    assert body["revision"] != body["operation"]["revision"]
    response = await post(client, body)
    assert response.status_code == 202 and response.json()["review_id"] == "new"
    assert response.json()["baseline"] == "updated"
    assert (await repo.get_deployment(response.json()["event_id"]))["payload"]["revision"] == body["revision"]
    repo.baselines[("sample-app", "aws")]["database_has_data"] = True
    assert (await post(client, body)).json()["duplicate"]
    assert (await repo.get_baseline("sample-app", "aws"))["database_has_data"] is True
    progress = (await client.get("/reviews/new/progress")).json()
    assert next(c for c in progress["envs"] if c["is_target"])["deploy"]["kind"] == "healthy"


async def test_error_without_operation_revision_is_unlinked_and_started_at_distinguishes_attempts(setup):
    repo, client, _ = setup
    body = payload(); body["operation"]["phase"] = "Error"; body["operation"]["revision"] = None
    body["operation"]["resources"] = []; body["images"] = ["org/app:" + "a"*40]
    first = (await post(client, body)).json()
    assert first["review_id"] is None
    body["operation"]["started_at"] = "2026-10-09T15:00:00+09:00"
    second = (await post(client, body)).json()
    assert second["event_id"] != first["event_id"]
    assert len(repo.analysis_jobs) == 2
    assert not await repo.get_failure_case(first["event_id"])
