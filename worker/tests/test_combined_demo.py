"""Three-branch integration: an autofix invalidates every old-head approval."""
import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver

from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.overlay.plan import strategy_ops
from review_ai.spec.deploy_spec import AppSpec
from review_api.app import create_app
from review_worker.graph import Deps, build_graph
from review_worker.handler import ReviewHandler
from tests.conftest import load_sample
from tests.test_deployment_requests import System, HEAD, FIX, REPO


@pytest.mark.parametrize("deterministic,strict", [(False, False), (True, False), (False, True), (True, True)])
async def test_busan_fix_rechecks_tokyo_approval_and_keeps_db_provisioning_guard(deterministic, strict):
    s = System()
    graph = build_graph(Deps(repo=s.repo, github=s.gh, publisher=s.publisher,
                            llm=ScriptedLLM(oracle_review), retriever=FileRetriever(),
                            deterministic_fixes=deterministic, strict_citations=strict), InMemorySaver())
    s.handler = ReviewHandler(s.repo, graph)
    busan = yaml.safe_load(s.gh.files[(HEAD, "deploy/local.yaml")])
    busan["runtime"]["resources"].pop("cpu_limit")
    busan["runtime"]["resources"].pop("memory_limit")
    s.gh.files[(HEAD, "deploy/local.yaml")] = yaml.safe_dump(busan)
    tokyo = yaml.safe_load(s.gh.files[(HEAD, "deploy/gcp.yaml")])
    sample = load_sample("24-human-tokyo-manual-bluegreen.yaml")
    for field in ("database", "secrets", "rollout"):
        tokyo[field] = sample[field]
    s.gh.files[(HEAD, "deploy/gcp.yaml")] = yaml.safe_dump(tokyo)

    async with AsyncClient(transport=ASGITransport(app=create_app(s.deps)), base_url="http://test",
                           headers={"Authorization": "Bearer test-token"}) as client:
        rid = await s.start()
        await s.work()
        children = {c["target_env"]: c for c in await s.repo.request_children(rid)}
        assert children["local"]["rounds"][0]["verdict"] == "fix"
        assert children["gcp"]["status"] == "needs_human"
        await s.coordinator.advance(rid)
        assert not s.gh.fixes and not s.gh.merges
        response = await client.post(f'/reviews/{children["gcp"]["review_id"]}/decision',
                                     json={"decision": "approved", "approver": "tester"})
        assert response.status_code == 202, response.text
        await s.work()
        await s.coordinator.advance(rid)
        assert len(s.gh.fixes) == 1 and set(s.gh.fixes[0]["files"]) == {"deploy/local.yaml"}
        assert not s.gh.merges
        newer = next(r for r in await s.repo.pending_requests())
        assert newer["head_sha"] == FIX
        await s.work()
        children = {c["target_env"]: c for c in await s.repo.request_children(newer["request_id"])}
        assert children["local"]["verdict"] == "pass"
        assert children["gcp"]["status"] == "needs_human"
        assert children["gcp"]["human_decision"] is None
        assert {c["pr_head_sha"] for c in children.values()} == {FIX}
        await s.coordinator.advance(newer["request_id"])
        assert not s.gh.merges
        response = await client.post(f'/reviews/{children["gcp"]["review_id"]}/decision',
                                     json={"decision": "approved", "approver": "tester"})
        assert response.status_code == 202, response.text
        await s.work()
        await s.coordinator.advance(newer["request_id"])
        parent = await s.repo.get_request(newer["request_id"])
        assert parent["state"] == "blocked" and "DB_PROVISIONING_REQUIRED" in parent["error"]
        assert not s.gh.merges and not s.gh.gitops_commits
        child = await s.repo.get_review(children["gcp"]["review_id"])
        bg = strategy_ops(AppSpec.model_validate(child["final_spec"]))[0]["value"]["blueGreen"]
        assert bg["autoPromotionEnabled"] is False and "autoPromotionSeconds" not in bg
