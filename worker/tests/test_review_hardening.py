from tests.conftest import FIX_SHA, Harness, load_sample, suite


async def test_deterministic_fix_follows_existing_commit_recheck_and_ci_path():
    harness = Harness(deterministic_fixes=True, strict_citations=True)
    await harness.request(load_sample("07-fix-public-bucket.yaml"))
    row = harness.row()
    assert row["status"] == "superseded" and row["rounds"][0]["patch_source"] == "deterministic"
    assert harness.deps.llm.calls == 0 and len(harness.github.commits) == 1
    new_id = row["superseded_by"]
    await harness.deliver_published()
    assert harness.row(new_id)["status"] == "waiting_ci" and not harness.github.merged
    harness.github.suites[FIX_SHA] = [suite()]
    await harness.ci(new_id, head_sha=FIX_SHA)
    assert harness.github.merged[0][2] == FIX_SHA


async def test_compatible_vs_strict_worker_citations_keep_rollout_control():
    from review_ai.judge.fake_llm import ScriptedLLM, oracle_review
    def legacy(request):
        result = oracle_review(request)
        for item in result["items"]:
            item.pop("cited_chunk_ids")
        return result
    compatible = Harness(llm=ScriptedLLM(legacy))
    await compatible.request(load_sample("07-fix-public-bucket.yaml"))
    assert compatible.row()["status"] == "superseded"
    strict = Harness(llm=ScriptedLLM(legacy), strict_citations=True)
    await strict.request(load_sample("07-fix-public-bucket.yaml"))
    assert strict.row()["status"] == "needs_human" and strict.row()["reasons"] == ["CITATION_INVALID"]
    assert not strict.github.commits and not strict.github.merged
