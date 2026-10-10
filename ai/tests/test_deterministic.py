"""허용 규칙의 확정값만 자동 적용되고 모든 기존 안전 게이트가 유지되는지 검증한다."""
import pytest

from review_ai.evaluation import build_spec, load_eval_cases
from review_ai.graph import initial_state, run_graph
from review_ai.judge.deterministic import deterministic_review
from review_ai.judge.fake_llm import FAKES
from review_ai.retrieval.file_retriever import FileRetriever
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import run_static_check
from tests.conftest import load_sample_dict


@pytest.mark.parametrize("case_id", ["s07-public-bucket", "p1-db006-public-managed-db", "p1-sto004-unencrypted-bucket"])
async def test_allowed_rule_reuses_recommendations_and_passes_without_llm(case_id):
    spec = build_spec(next(c for c in load_eval_cases() if c["id"] == case_id))
    llm = FAKES["oracle"]()
    final = await run_graph(initial_state(spec, review_id="det"), llm=llm, retriever=FileRetriever(),
                            deterministic_fixes=True, strict_citations=True)
    assert final["status"] == "pass" and llm.calls == 0 and final["patched"]
    snapshot = final["rounds"][0]
    assert snapshot["patch_source"] == "deterministic"
    assert snapshot["validation"]["deterministic_available"] and not snapshot["validation"]["llm_available"]
    assert all(i["evidence"] and i["why"] for i in snapshot["items"])


async def test_deterministic_flag_is_off_by_default():
    final = await run_graph(initial_state(load_sample_dict("07-fix-public-bucket.yaml"), review_id="det"),
                            llm=None, retriever=FileRetriever())
    assert final["status"] == "needs_human" and "LLM_UNAVAILABLE" in final["decision"]["reasons"]


@pytest.mark.parametrize("flag", ["generated_spec", "autofix_commit", "human_decision"])
async def test_deterministic_cannot_bypass_generated_bot_or_human_guards(flag):
    state = initial_state(load_sample_dict("07-fix-public-bucket.yaml"), review_id="det")
    state[flag] = {"decision": "approved", "approver": "test", "edited_ops": [], "use_recommendations": False} if flag == "human_decision" else True
    final = await run_graph(state, llm=None, retriever=FileRetriever(), deterministic_fixes=True)
    assert final["status"] == "needs_human" and not final["applied_ops"]


@pytest.mark.parametrize("sample", ["10-human-mixed-aws.yaml", "03-fix-sqlite-replicas-gcp.yaml"])
async def test_mixed_or_unlisted_findings_still_need_llm_or_human(sample):
    final = await run_graph(initial_state(load_sample_dict(sample), review_id="det"), llm=None,
                            retriever=FileRetriever(), deterministic_fixes=True)
    assert final["status"] == "needs_human" and not final["applied_ops"]


async def test_rule_document_missing_cannot_use_deterministic_patch():
    class Empty:
        async def search(self, *args):
            return []
    final = await run_graph(initial_state(load_sample_dict("07-fix-public-bucket.yaml"), review_id="det"),
                            llm=None, retriever=Empty(), deterministic_fixes=True)
    assert "LOW_SCORE" in final["decision"]["reasons"] and not final["applied_ops"]


async def test_deterministic_rejects_baseline_candidates_and_out_of_scope_ops(monkeypatch):
    import review_ai.judge.deterministic as module
    spec = load_sample_dict("07-fix-public-bucket.yaml")
    findings = run_static_check(DeploySpec.model_validate(spec))
    docs = await FileRetriever().search(findings, "aws")
    candidate = {"finding_ids": [findings[0]["finding_id"]], "source": "baseline", "why": "test",
                 "ops": [{"op": "replace", "path": "/runtime/replicas", "value": 1}]}
    monkeypatch.setattr(module, "build_recommendations", lambda *_: [candidate])
    assert deterministic_review(spec, findings, docs) is None
    candidate["source"] = "rule"
    assert deterministic_review(spec, findings, docs) is None


async def test_deterministic_cannot_exceed_existing_patch_size_limit():
    spec = load_sample_dict("07-fix-public-bucket.yaml")
    spec["storage"]["buckets"] = [dict(name=f"bucket-{i}", public=True) for i in range(11)]
    findings = run_static_check(DeploySpec.model_validate(spec))
    docs = await FileRetriever().search(findings, "aws")
    assert deterministic_review(spec, findings, docs) is None
