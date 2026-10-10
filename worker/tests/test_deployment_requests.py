from __future__ import annotations

import asyncio
import copy
from pathlib import Path

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
import pytest
import yaml

from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.messages import ReviewRequested
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.overlay import render_overlay
from review_ai.spec.deploy_spec import AppSpec
from review_ai.spec.deployment_request import DeploymentRequest
from review_api.app import ApiDeps, create_app, start_deployment_request
from review_common.github import GitHubError, GitHubGitClient, RefConflict, SpecNotFound, git_blob_sha
from review_common.repository import InMemoryReviewRepository
from review_common.request_dispatch import dispatch_request
from review_worker.graph import Deps, build_graph
from review_worker.handler import ReviewHandler
from review_worker.request_coordinator import RequestCoordinator
from review_worker.request_gitops import RequestGitOps
from tests.conftest import FakePublisher, load_sample, suite

REPO = "Crystal-SBHackathon2026/sample-app"
GITOPS = "Crystal-SBHackathon2026/gitops"
HEAD, FIX, MERGE = "a" * 40, "b" * 40, "c" * 40
REGISTRY = frozenset({("aws", "ap-northeast-2", "in-cluster"), ("gcp", "asia-northeast1", "tokyo-gke"),
                      ("local", "busan-local", "crystal-busan")})
FIXTURES = Path(__file__).parent / "fixtures/multi-target/gitops"


class GitHub:
    """Offline Git Data API, PR, and CI adapter; production renderer/Kustomize are used unchanged."""
    def __init__(self, envs=("aws", "gcp", "local")):
        self.files, self.snapshots, self.trees = {}, {}, {}
        self.gitops_head = "d" * 40
        snapshot = {str(p.relative_to(FIXTURES)): p.read_text() for p in FIXTURES.rglob("*.yaml")}
        self.snapshots[self.gitops_head] = snapshot
        self.pull = dict(number=7, state="open", draft=False,
                         head=dict(sha=HEAD, ref="feature", repo=dict(full_name=REPO)),
                         base=dict(ref="main", repo=dict(full_name=REPO)))
        self.merges, self.fixes, self.gitops_commits, self.statuses = [], [], [], []
        self.image_ready, self.head_ci = True, "success"
        self.crash_after_merge = False
        targets = []
        for env in envs:
            _, region, cluster = next(t for t in REGISTRY if t[0] == env)
            spec = load_sample("01-pass-sample-app-aws.yaml")
            spec["target"] = dict(env=env, region=region, namespace="sample-app")
            spec["runtime"]["env"] = dict(DEPLOY_ENV=env, DEPLOY_REGION=region)
            # Preserve the manual demo ingress and GCP bluegreen delay.
            if env == "gcp":
                spec.pop("network", None)
                spec["rollout"] = dict(strategy="bluegreen", auto_promotion_seconds=30)
            self.files[(HEAD, f"deploy/{env}.yaml")] = yaml.safe_dump(spec)
            targets.append(dict(env=env, region=region, cluster=cluster, path=f"deploy/{env}.yaml"))
        manifest = dict(apiVersion="oneaction/v1", kind="DeploymentRequest", targets=targets)
        self.files[(HEAD, "deployment-request.yaml")] = yaml.safe_dump(manifest)

    async def get_file(self, repo, path, ref, **kwargs):
        try:
            return self.snapshots[ref][path] if repo == GITOPS else self.files[(ref, path)]
        except KeyError:
            raise SpecNotFound("missing spec", 404) from None

    async def get_pull(self, repo, number):
        return copy.deepcopy(self.pull)

    async def create_commit_status(self, repo, sha, **fields):
        self.statuses.append((sha, fields["state"]))

    async def check_suites(self, repo, sha):
        return [suite(conclusion=self.head_ci)] if sha != MERGE else ([suite()] if self.image_ready else [])

    async def merge_pull(self, repo, number, *, head_sha):
        assert self.pull["head"]["sha"] == head_sha
        self.merges.append(head_sha)
        self.pull.update(state="closed", merged=True, merged_at="now", merge_commit_sha=MERGE)
        if self.crash_after_merge:
            self.crash_after_merge = False
            raise RuntimeError("process died after merge")
        return MERGE

    async def prepare_files_commit(self, repo, *, parent, files, message):
        self.fixes.append(dict(files=files, parent=parent))
        for (sha, path), text in list(self.files.items()):
            if sha == parent:
                self.files[(FIX, path)] = text
        for path, text in files.items():
            self.files[(FIX, path)] = text
        return FIX

    async def update_branch(self, repo, branch, sha):
        if repo == GITOPS:
            self.gitops_head = sha
        else:
            self.pull["head"]["sha"] = sha

    async def branch_sha(self, repo, branch):
        return self.gitops_head

    async def commit_tree_sha(self, repo, head):
        return head

    async def tree_blobs(self, repo, tree):
        return {p: git_blob_sha(v) for p, v in self.snapshots[tree].items()}

    async def create_tree(self, repo, base, entries):
        snap = dict(self.snapshots[base])
        for entry in entries:
            if entry.get("sha", "present") is None:
                snap.pop(entry["path"], None)
            else:
                snap[entry["path"]] = entry["content"]
        tree = f"tree-{len(self.trees)}"
        self.trees[tree] = snap
        return tree

    async def create_commit(self, repo, *, message, tree, parents):
        sha = f"{len(self.gitops_commits) + 1:040x}"
        self.snapshots[sha] = self.trees[tree]
        self.gitops_commits.append(dict(sha=sha, message=message, parents=parents))
        return sha


class System:
    def __init__(self, envs=("aws", "gcp", "local")):
        self.repo, self.gh, self.publisher = InMemoryReviewRepository(), GitHub(envs), FakePublisher()
        graph = build_graph(Deps(repo=self.repo, github=self.gh, publisher=self.publisher,
                                 llm=ScriptedLLM(oracle_review), retriever=FileRetriever()), InMemorySaver())
        self.handler = ReviewHandler(self.repo, graph)
        self.gitops = RequestGitOps(GitHubGitClient(self.gh, gitops_repo=GITOPS))
        self.coordinator = RequestCoordinator(self.repo, self.gh, self.publisher, self.gitops, REGISTRY)
        self.deps = ApiDeps(repo=self.repo, specs=self.gh, publisher=self.publisher, github=self.gh,
                            multi_target_enabled=True, deployment_targets=REGISTRY, api_token="test-token")

    async def start(self):
        return await dispatch_request(self.repo, self.gh, self.publisher, REPO, 7, HEAD,
                                      "deployment-request.yaml", "tester", REGISTRY)

    async def work(self):
        messages, self.publisher.sent = self.publisher.sent, []
        for topic, _, value in messages:
            await self.handler.handle(topic, value)


@pytest.mark.asyncio
async def test_three_reviews_merge_once_and_one_atomic_release():
    s = System()
    rid = await s.start()
    await s.work()
    children = await s.repo.request_children(rid)
    assert len(children) == 3
    assert {c["status"] for c in children} == {"waiting_ci"}
    assert not s.gh.merges and not s.gh.fixes and not s.gh.statuses
    await s.coordinator.advance(rid)
    assert s.gh.merges == [HEAD]
    assert len(s.gh.gitops_commits) == 1
    assert (await s.repo.get_request(rid))["state"] == "committed"
    snap = s.gh.snapshots[s.gh.gitops_head]
    for env in ("aws", "gcp", "local"):
        k = yaml.safe_load(snap[f"apps/sample-app/overlays/{env}/kustomization.yaml"])
        assert k["images"][0]["newTag"] == MERGE
    gcp = yaml.safe_load(snap["apps/sample-app/overlays/gcp/kustomization.yaml"])
    ops = yaml.safe_load(gcp["patches"][0]["patch"])
    strategy = next(p["value"] for p in ops if p["path"] == "/spec/strategy")
    assert strategy["blueGreen"]["autoPromotionSeconds"] == 30
    await s.coordinator.advance(rid)
    assert len(s.gh.merges) == len(s.gh.gitops_commits) == 1


@pytest.mark.asyncio
async def test_gcp_only_keeps_unselected_resolved_configuration(tmp_path):
    s = System(("gcp",))
    before = copy.deepcopy(s.gh.snapshots[s.gh.gitops_head])
    rid = await s.start()
    await s.work()
    await s.coordinator.advance(rid)
    after = s.gh.snapshots[s.gh.gitops_head]
    assert before["apps/sample-app/base/kustomization.yaml"] == after["apps/sample-app/base/kustomization.yaml"]
    # Compare effective manifests, not serialization/comments; pinning must preserve runtime versions.
    import subprocess
    for revision, snapshot in (("before", before), ("after", after)):
        for path, text in snapshot.items():
            dest = tmp_path / revision / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
    for env in ("aws", "local"):
        outputs = [subprocess.check_output(["kubectl", "kustomize", str(tmp_path / rev / f"apps/sample-app/overlays/{env}")])
                   for rev in ("before", "after")]
        assert outputs[0] == outputs[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["needs_human", "rejected", "failed"])
async def test_one_unapproved_environment_holds_merge(state):
    s = System()
    rid = await s.start()
    await s.work()
    child = (await s.repo.request_children(rid))[0]
    await s.repo.update_review(child["review_id"], status=state)
    await s.coordinator.advance(rid)
    assert not s.gh.merges and not s.gh.gitops_commits


@pytest.mark.asyncio
async def test_changes_aggregate_and_every_environment_reviews_new_sha():
    s = System()
    rid = await s.start()
    await s.work()
    for child in (await s.repo.request_children(rid))[:2]:
        await s.repo.update_review(child["review_id"], rounds=[{"patch": {"ops": [
            dict(op="add", path="/runtime/termination_grace_seconds", value=45)]}}])
    await s.coordinator.advance(rid)
    assert len(s.gh.fixes) == 1 and len(s.gh.fixes[0]["files"]) == 2
    assert not s.gh.merges
    assert (await s.repo.get_request(rid))["state"] == "superseded"
    newer = next(r for r in await s.repo.pending_requests())
    assert newer["head_sha"] == FIX
    children = await s.repo.request_children(newer["request_id"])
    assert len(children) == 3 and {c["pr_head_sha"] for c in children} == {FIX}
    assert all(c["human_decision"] is None for c in children)
    await s.work()
    await s.coordinator.advance(newer["request_id"])
    assert s.gh.merges == [FIX]


@pytest.mark.asyncio
async def test_crash_after_merge_resumes_without_second_merge():
    s = System()
    rid = await s.start()
    await s.work()
    s.gh.crash_after_merge = True
    await s.coordinator.advance(rid)
    assert (await s.repo.get_request(rid))["state"] == "merging"
    await s.coordinator.advance(rid)
    assert s.gh.merges == [HEAD] and len(s.gh.gitops_commits) == 1


@pytest.mark.asyncio
async def test_ci_of_merge_image_required():
    s = System(("gcp",))
    rid = await s.start()
    await s.work()
    s.gh.image_ready = False
    await s.coordinator.advance(rid)
    assert s.gh.merges == [HEAD] and not s.gh.gitops_commits
    assert (await s.repo.get_request(rid))["state"] == "waiting_image"
    s.gh.image_ready = True
    await s.coordinator.advance(rid)
    assert len(s.gh.gitops_commits) == 1


@pytest.mark.asyncio
async def test_protected_ingress_removal_blocks_before_merge():
    s = System(("aws",))
    spec = yaml.safe_load(s.gh.files[(HEAD, "deploy/aws.yaml")])
    spec.pop("network")
    s.gh.files[(HEAD, "deploy/aws.yaml")] = yaml.safe_dump(spec)
    rid = await s.start()
    await s.work()
    await s.coordinator.advance(rid)
    assert not s.gh.merges
    assert (await s.repo.get_request(rid))["state"] == "blocked"


@pytest.mark.asyncio
async def test_duplicate_requests_and_concurrent_advances_are_idempotent():
    s = System()
    ids = await asyncio.gather(s.start(), s.start())
    assert ids[0] == ids[1] and len(s.repo.reviews) == 3
    await s.work()
    await asyncio.gather(s.coordinator.advance(ids[0]), s.coordinator.advance(ids[0]))
    assert len(s.gh.merges) == len(s.gh.gitops_commits) == 1


@pytest.mark.asyncio
async def test_draft_and_changed_head_cannot_merge():
    s = System()
    rid = await s.start()
    await s.work()
    s.gh.pull["draft"] = True
    await s.coordinator.advance(rid)
    assert not s.gh.merges
    s.gh.pull["draft"] = False
    s.gh.pull["head"]["sha"] = "e" * 40
    await s.coordinator.advance(rid)
    assert not s.gh.merges and (await s.repo.get_request(rid))["state"] == "superseded"


@pytest.mark.asyncio
async def test_gitops_changed_after_review_never_overwritten():
    s = System(("gcp",))
    rid = await s.start()
    await s.work()
    s.gh.image_ready = False
    await s.coordinator.advance(rid)
    # Another release or operator changes a selected environment while image CI runs.
    snap = s.gh.snapshots[s.gh.gitops_head]
    snap["apps/sample-app/overlays/gcp/kustomization.yaml"] += "\ncommonLabels:\n  operator: changed\n"
    s.gh.image_ready = True
    await s.coordinator.advance(rid)
    assert not s.gh.gitops_commits
    assert (await s.repo.get_request(rid))["state"] == "blocked"


@pytest.mark.asyncio
async def test_partial_failure_retry_changes_only_failed_environment():
    s = System()
    rid = await s.start()
    await s.work()
    await s.coordinator.advance(rid)
    before = copy.deepcopy(s.gh.snapshots[s.gh.gitops_head])
    for child in await s.repo.request_children(rid):
        await s.repo.add_deploy_event(review_id=child["review_id"], app=child["app"], target_env=child["target_env"],
                                      kind="degraded" if child["target_env"] == "gcp" else "healthy",
                                      image_tag=MERGE, payload={})
    client = TestClient(create_app(s.deps), headers={"Authorization": "Bearer test-token"})
    assert client.get(f"/deployment-requests/{rid}").json()["deployment"] == "partial_failure"
    assert client.post(f"/deployment-requests/{rid}/retry", json=dict(env="aws", idempotency_key="one")).status_code == 409
    body = dict(env="gcp", idempotency_key="one")
    result = client.post(f"/deployment-requests/{rid}/retry", json=body)
    assert result.status_code == 202
    again = client.post(f"/deployment-requests/{rid}/retry", json=body)
    assert again.json()["retry_id"] == result.json()["retry_id"]
    await s.coordinator.retry(result.json())
    after = s.gh.snapshots[s.gh.gitops_head]
    changed = {p for p in after if after[p] != before.get(p)}
    assert changed == {"apps/sample-app/overlays/gcp/kustomization.yaml"}
    assert len(s.gh.merges) == 1
    assert client.get(f"/deployment-requests/{rid}").json()["deployment"] == "partial_failure"


@pytest.mark.asyncio
async def test_api_requires_enabled_registered_destinations_and_auth():
    s = System(("gcp",))
    client = TestClient(create_app(s.deps))
    body = dict(spec_ref=dict(repository=REPO, commit=HEAD, path="deployment-request.yaml"), pr_number=7, requested_by="user")
    assert client.post("/deployment-requests", json=body).status_code == 401
    client.headers["Authorization"] = "Bearer test-token"
    s.deps.multi_target_enabled = False
    assert client.post("/deployment-requests", json=body).status_code == 409
    s.deps.multi_target_enabled = True
    s.deps.deployment_targets = frozenset({("aws", "ap-northeast-2", "in-cluster")})
    assert client.post("/deployment-requests", json=body).status_code == 422
    assert not s.repo.requests
    s.deps.deployment_targets = REGISTRY
    assert client.post("/deployment-requests", json=body).status_code == 202


@pytest.mark.asyncio
async def test_no_group_cross_environment_event_fallback():
    s = System(("gcp",))
    rid = await s.start()
    await s.work()
    await s.coordinator.advance(rid)
    assert await s.repo.find_by_merge_sha(app="sample-app", target_env=None, image_tag=MERGE) is None
    assert await s.repo.find_by_merge_sha(app="sample-app", target_env="gcp", image_tag=MERGE)


@pytest.mark.asyncio
async def test_legacy_overlay_write_preserves_ci_owned_environment_image():
    s = System(("gcp",))
    rid = await s.start()
    await s.work()
    await s.coordinator.advance(rid)
    snap = s.gh.snapshots[s.gh.gitops_head]
    path = "apps/sample-app/overlays/aws/kustomization.yaml"
    k = yaml.safe_load(snap[path])
    k["images"][0]["newTag"] = "f" * 40  # single-env CI has updated the AWS pin
    snap[path] = yaml.safe_dump(k)
    spec = AppSpec.model_validate(load_sample("01-pass-sample-app-aws.yaml"))
    rendered = render_overlay(spec)
    git = GitHubGitClient(s.gh, gitops_repo=GITOPS, preserve_overlay_images=True)
    await git.commit_files(rendered.directory, rendered.files, "legacy overlay update")
    updated = yaml.safe_load(s.gh.snapshots[s.gh.gitops_head][path])
    assert updated["images"][0]["newTag"] == "f" * 40


@pytest.mark.asyncio
async def test_missing_selected_review_does_not_count_as_pass():
    s = System()
    rid = await s.start()
    await s.work()
    child = (await s.repo.request_children(rid))[0]
    s.repo.reviews.pop(child["review_id"])
    await s.coordinator.advance(rid)
    assert not s.gh.merges
    assert len(await s.repo.request_children(rid)) == 3
    assert "received" in {c["status"] for c in await s.repo.request_children(rid)}


@pytest.mark.asyncio
async def test_duplicate_old_head_webhook_preserves_prepared_fix_successor():
    s = System()
    rid = await s.start()
    await s.work()
    await s.gh.prepare_files_commit(REPO, parent=HEAD, files={"deploy/aws.yaml": s.gh.files[(HEAD, "deploy/aws.yaml")]},
                                   message="prepared fix")
    await s.repo.update_request(rid, state="fixing", pending_sha=FIX)
    successor = await dispatch_request(s.repo, s.gh, s.publisher, REPO, 7, FIX, "deployment-request.yaml",
                                       "autofix:user", REGISTRY, fix_count=1)
    result = await start_deployment_request(s.deps, dict(repository=REPO, commit=HEAD, path="deployment-request.yaml"),
                                            7, "duplicate-webhook")
    assert result["request_id"] == rid
    assert (await s.repo.get_request(successor))["state"] == "reviewing"
    assert {c["status"] for c in await s.repo.request_children(successor)} == {"received"}
    await s.coordinator.advance(rid)
    assert s.gh.pull["head"]["sha"] == FIX
    assert (await s.repo.get_request(rid))["state"] == "superseded"


@pytest.mark.asyncio
@pytest.mark.parametrize("ref_result", ["not_updated", "reply_lost", "changed_head"])
async def test_fix_ref_failure_recovers_or_retires_only_a_changed_pr(ref_result):
    s = System(("gcp",))
    rid = await s.start()
    await s.work()
    child = (await s.repo.request_children(rid))[0]
    await s.repo.update_review(child["review_id"], rounds=[{"patch": {"ops": [
        dict(op="add", path="/runtime/termination_grace_seconds", value=45)]}}])
    update_branch = s.gh.update_branch

    async def failed_ref(repository, branch, sha):
        if repository == REPO:
            if ref_result == "reply_lost":
                await update_branch(repository, branch, sha)
            elif ref_result == "changed_head":
                s.gh.pull["head"]["sha"] = "e" * 40
                raise RefConflict("operator changed the PR head", 422)
            raise GitHubError("temporary network failure")
        await update_branch(repository, branch, sha)

    s.gh.update_branch = failed_ref
    await s.coordinator.advance(rid)
    successor = next(r for r in s.repo.requests.values() if r["request_id"] != rid)
    s.gh.update_branch = update_branch
    if ref_result == "changed_head":
        assert (await s.repo.get_request(rid))["state"] == "superseded"
        assert (await s.repo.get_request(successor["request_id"]))["state"] == "superseded"
        assert s.gh.pull["head"]["sha"] == "e" * 40
        assert not s.gh.merges
        return
    if ref_result == "not_updated":
        assert (await s.repo.get_request(rid))["state"] == "fixing"
        # A restarted worker may poll the prepared successor before its predecessor.
        await s.coordinator.advance(successor["request_id"])
        assert (await s.repo.get_request(successor["request_id"]))["state"] == "reviewing"
        assert not s.gh.merges
        await s.coordinator.advance(rid)
    assert s.gh.pull["head"]["sha"] == FIX
    assert (await s.repo.get_request(rid))["state"] == "superseded"
    await s.work()
    await s.coordinator.advance(successor["request_id"])
    assert (await s.repo.get_request(successor["request_id"]))["state"] == "committed"
    assert len(s.gh.fixes) == 1 and s.gh.merges == [FIX]


@pytest.mark.asyncio
async def test_retry_preserves_operator_configuration_and_is_idempotent():
    s = System()
    rid = await s.start()
    await s.work()
    await s.coordinator.advance(rid)
    path = "apps/sample-app/overlays/gcp/kustomization.yaml"
    live = s.gh.snapshots[s.gh.gitops_head]
    config = yaml.safe_load(live[path])
    config["commonAnnotations"] = {"operator-emergency": "keep"}
    config["patches"].append({"target": {"kind": "Rollout", "name": "sample-app"}, "patch": yaml.safe_dump({
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Rollout", "metadata": {"name": "sample-app"},
        "spec": {"replicas": 5}})})
    config["resources"].append("operator-config.yaml")
    live[path] = yaml.safe_dump(config, sort_keys=False)
    live["apps/sample-app/overlays/gcp/operator-config.yaml"] = yaml.safe_dump({
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "operator-config"},
        "data": {"emergency": "keep"}})
    before = copy.deepcopy(live)
    child = next(c for c in await s.repo.request_children(rid) if c["target_env"] == "gcp")
    await s.repo.add_deploy_event(review_id=child["review_id"], app=child["app"], target_env="gcp",
                                 kind="degraded", image_tag=MERGE, payload={})
    retry = await s.repo.insert_request_retry("retry_preserve_configuration", rid, "gcp")
    await s.coordinator.retry(retry)
    after = s.gh.snapshots[s.gh.gitops_head]
    assert {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)} == {path}
    updated = yaml.safe_load(after[path])
    assert {k: v for k, v in updated.items() if k != "patches"} == {k: v for k, v in config.items() if k != "patches"}
    assert updated["patches"][:-1] == config["patches"]
    patch = yaml.safe_load(updated["patches"][-1]["patch"])
    assert patch["spec"]["template"]["metadata"]["annotations"]["oneaction.crystal/retry"] == retry["retry_id"]
    commits = len(s.gh.gitops_commits)
    await s.coordinator.retry(retry)
    assert len(s.gh.gitops_commits) == commits


@pytest.mark.asyncio
async def test_retry_does_not_pin_or_change_unselected_environments():
    s = System(("gcp",))
    rid = await s.start()
    await s.work()
    await s.coordinator.advance(rid)
    live = s.gh.snapshots[s.gh.gitops_head]
    for env in ("aws", "local"):
        path = f"apps/sample-app/overlays/{env}/kustomization.yaml"
        config = yaml.safe_load(live[path])
        config.pop("images", None)  # Operator has restored inheritance from base.
        live[path] = yaml.safe_dump(config, sort_keys=False)
    before = copy.deepcopy(live)
    retry = await s.repo.insert_request_retry("retry_no_other_changes", rid, "gcp")
    await s.coordinator.retry(retry)
    after = s.gh.snapshots[s.gh.gitops_head]
    assert {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)} == {
        "apps/sample-app/overlays/gcp/kustomization.yaml"}


@pytest.mark.asyncio
@pytest.mark.parametrize("new_version", ["tag", "digest"])
async def test_retry_cannot_revert_a_different_running_image(new_version):
    s = System(("gcp",))
    rid = await s.start()
    await s.work()
    await s.coordinator.advance(rid)
    path = "apps/sample-app/overlays/gcp/kustomization.yaml"
    live = s.gh.snapshots[s.gh.gitops_head]
    config = yaml.safe_load(live[path])
    if new_version == "tag":
        config["images"][0]["newTag"] = "f" * 40
    else:
        config["images"][0]["digest"] = "sha256:" + "f" * 64
    live[path] = yaml.safe_dump(config, sort_keys=False)
    before = copy.deepcopy(live)
    retry = await s.repo.insert_request_retry("retry_different_image", rid, "gcp")
    await s.coordinator.retry(retry)
    assert s.gh.snapshots[s.gh.gitops_head] == before
    assert s.repo.request_retries[retry["retry_id"]]["state"] == "blocked"


@pytest.mark.asyncio
async def test_prepared_fix_is_held_while_pr_is_draft():
    s = System(("gcp",))
    rid = await s.start()
    await s.gh.prepare_files_commit(REPO, parent=HEAD, files={"deploy/gcp.yaml": s.gh.files[(HEAD, "deploy/gcp.yaml")]},
                                   message="prepared fix")
    await s.repo.update_request(rid, state="fixing", pending_sha=FIX)
    s.gh.pull["draft"] = True
    await s.coordinator.advance(rid)
    assert s.gh.pull["head"]["sha"] == HEAD
    assert (await s.repo.get_request(rid))["state"] == "fixing"
    s.gh.pull["draft"] = False
    await s.coordinator.advance(rid)
    assert s.gh.pull["head"]["sha"] == FIX
    assert (await s.repo.get_request(rid))["state"] == "superseded"


def test_api_rejects_a_pr_targeting_a_non_deployment_branch():
    s = System(("gcp",))
    s.gh.pull["base"]["ref"] = "staging"
    client = TestClient(create_app(s.deps), headers={"Authorization": "Bearer test-token"})
    body = dict(spec_ref=dict(repository=REPO, commit=HEAD, path="deployment-request.yaml"),
                pr_number=7, requested_by="user")
    assert client.post("/deployment-requests", json=body).status_code == 409
    assert not s.repo.requests and not s.publisher.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("retarget_at", ["before_review", "preflight", "ci_confirmation"])
async def test_coordinator_rechecks_deployment_branch_before_merging(retarget_at):
    s = System(("gcp",))
    rid = await s.start()
    await s.work()
    if retarget_at == "preflight":
        preflight = s.gitops.preflight

        async def retarget(*args):
            snapshot = await preflight(*args)
            s.gh.pull["base"]["ref"] = "staging"
            return snapshot

        s.gitops.preflight = retarget
    elif retarget_at == "ci_confirmation":
        check_suites = s.gh.check_suites
        checks = 0

        async def retarget(repository, sha):
            nonlocal checks
            checks += 1
            if checks == 2:
                s.gh.pull["base"]["ref"] = "staging"
            return await check_suites(repository, sha)

        s.gh.check_suites = retarget
    else:
        s.gh.pull["base"]["ref"] = "staging"
    await s.coordinator.advance(rid)
    assert not s.gh.merges and not s.gh.gitops_commits
    assert (await s.repo.get_request(rid))["state"] in {"blocked", "superseded"}


@pytest.mark.asyncio
async def test_api_rejects_custom_manifest_path_not_recognized_by_image_ci():
    s = System()
    client = TestClient(create_app(s.deps), headers={"Authorization": "Bearer test-token"})
    body = dict(spec_ref=dict(repository=REPO, commit=HEAD, path="custom-request.yaml"),
                pr_number=7, requested_by="user")
    assert client.post("/deployment-requests", json=body).status_code == 422
    assert not s.repo.requests


def test_legacy_api_cannot_bypass_explicit_environment_selection():
    s = System(("gcp",))
    client = TestClient(create_app(s.deps), headers={"Authorization": "Bearer test-token"})
    body = dict(spec_ref=dict(repository=REPO, commit=HEAD, path="deploy/gcp.yaml"), requested_by="user")
    assert client.post("/reviews", json=body).status_code == 409
    assert not s.repo.reviews


@pytest.mark.parametrize("path", ["../deploy.yaml", "/deploy.yaml", "deploy.yaml?ref=main", "x/../../deploy.yaml"])
def test_manifest_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        DeploymentRequest.model_validate(dict(apiVersion="oneaction/v1", kind="DeploymentRequest", targets=[
            dict(env="gcp", region="asia-northeast1", cluster="tokyo-gke", path=path)]))
