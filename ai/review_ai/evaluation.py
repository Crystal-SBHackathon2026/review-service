"""환각 평가셋 실행 — eval/cases.yaml 의 케이스를 run_graph 로 돌려 기대 결과와 비교한다.

지표: 기대 verdict 일치율 · 인용 유효율 · 정상(pass 기대) 케이스 오탐 · LLM 호출 수·토큰·지연.
LLM 은 같은 입력에도 출력이 흔들리므로 실제 LLM 평가는 케이스마다 repeat 회 돌린다.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from review_ai.graph import initial_state, run_graph
from review_ai.judge.fake_llm import FAKES
from review_ai.judge.llm import LlmClient, LlmResponse
from review_ai.judge.prompt import JudgeRequest
from review_ai.patching import apply_ops
from review_ai.retrieval import Retriever

AI_ROOT = Path(__file__).resolve().parent.parent
EVAL_CASES = AI_ROOT / "eval" / "cases.yaml"
SAMPLES = AI_ROOT / "samples"
REVIEWER = "reviewer"


class RecordingLLM:
    """요청·응답을 기록한다. 프롬프트에 비밀 값이 들어갔는지, 몇 번 불렀는지 보는 데 쓴다."""

    def __init__(self, inner: LlmClient) -> None:
        self._inner = inner
        self.model = inner.model
        self.requests: list[JudgeRequest] = []
        self.responses: list[LlmResponse] = []

    async def complete(self, request: JudgeRequest) -> LlmResponse:
        self.requests.append(request)
        response = await self._inner.complete(request)
        self.responses.append(response)
        return response


@dataclass
class CaseResult:
    case_id: str
    llm_role: str
    ok: bool
    failures: list[str]
    verdict: str
    reasons: list[str]
    llm_calls: int
    citations_ok: bool | None
    usage: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


def load_eval_cases(path: Path = EVAL_CASES) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]


def build_spec(case: dict[str, Any]) -> dict[str, Any]:
    spec = yaml.safe_load((SAMPLES / case["sample"]).read_text(encoding="utf-8"))
    ops = copy.deepcopy(case.get("mutate") or [])
    for op in ops:
        value = op.get("value")
        if isinstance(value, dict) and value.get("spec") == "SELF":
            value["spec"] = {k: v for k, v in spec.items() if k != "baseline"}
    return apply_ops(spec, ops)


def _check(case: dict[str, Any], spec: dict[str, Any], final: dict[str, Any], llm: RecordingLLM) -> list[str]:
    exp, decision = case["expect"], final["decision"]
    first_findings = final["rounds"][0]["finding_ids"] if final["rounds"] else [f["finding_id"] for f in final["findings"]]
    failures = []
    if decision["verdict"] != exp["verdict"]:
        failures.append(f"verdict {decision['verdict']} != {exp['verdict']} (사유 {decision['reasons']})")
    missing = set(exp.get("reasons_include", [])) - set(decision["reasons"])
    if missing:
        failures.append(f"사유 누락 {sorted(missing)} (실제 {decision['reasons']})")
    if "findings" in exp and sorted(fid.split(":")[0] for fid in first_findings) != sorted(exp["findings"]):
        failures.append(f"findings {first_findings} != {exp['findings']}")
    if "llm_calls" in exp and len(llm.requests) != exp["llm_calls"]:
        failures.append(f"LLM 호출 {len(llm.requests)} != {exp['llm_calls']}")
    if "rounds" in exp and len(final["rounds"]) != exp["rounds"]:
        failures.append(f"패치 회차 {len(final['rounds'])} != {exp['rounds']}")
    for secret in exp.get("prompt_must_not_contain", []):
        if any(secret in r.user or secret in r.system for r in llm.requests):
            failures.append("프롬프트에 비밀 값이 들어갔다")
    if exp.get("no_engine_change") and final["deploy_spec"]["database"]["engine"] != spec["database"]["engine"]:
        failures.append("DB 엔진이 바뀌었다")
    return failures


def _usage(llm: RecordingLLM) -> dict[str, int]:
    total: dict[str, int] = {}
    for response in llm.responses:
        for key, value in response.usage.items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total


async def run_case(case: dict[str, Any], reviewer: Callable[[], LlmClient], retriever: Retriever) -> CaseResult:
    role = case["llm"]
    llm = RecordingLLM(reviewer() if role == REVIEWER else FAKES[role]())
    spec = build_spec(case)
    started = time.perf_counter()
    final = await run_graph(initial_state(spec, review_id=f"eval-{case['id']}"), llm=llm, retriever=retriever)
    decision = final["decision"]
    citations = decision["validation"].get("citations_ok") if decision["validation"].get("llm_available") else None
    failures = _check(case, spec, final, llm)
    return CaseResult(
        case_id=case["id"], llm_role=role, ok=not failures, failures=failures,
        verdict=decision["verdict"], reasons=decision["reasons"], llm_calls=len(llm.requests),
        citations_ok=citations, usage=_usage(llm), seconds=time.perf_counter() - started,
    )


def summarize(results: Sequence[CaseResult]) -> dict[str, Any]:
    reviewer = [r for r in results if r.llm_role == REVIEWER]
    cited = [r.citations_ok for r in reviewer if r.citations_ok is not None]
    pass_expected = {c["id"] for c in load_eval_cases() if c["expect"]["verdict"] == "pass"}
    false_positive = [r for r in results if r.case_id in pass_expected and r.verdict != "pass"]
    tokens: dict[str, int] = {}
    for r in results:
        for k, v in r.usage.items():
            tokens[k] = tokens.get(k, 0) + v
    return {
        "cases": len(results),
        "match_rate": sum(r.ok for r in results) / len(results) if results else 0.0,
        "reviewer_match_rate": sum(r.ok for r in reviewer) / len(reviewer) if reviewer else 0.0,
        "gate_match_rate": (sum(r.ok for r in results if r.llm_role != REVIEWER)
                            / max(1, sum(1 for r in results if r.llm_role != REVIEWER))),
        "citation_valid_rate": sum(cited) / len(cited) if cited else None,
        "false_positive_on_pass": len(false_positive),
        "llm_calls": sum(r.llm_calls for r in results),
        "tokens": tokens,
        "seconds": round(sum(r.seconds for r in results), 2),
    }
