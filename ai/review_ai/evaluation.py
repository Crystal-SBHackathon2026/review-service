"""환각 평가셋 실행 — eval/cases.yaml 의 케이스를 run_graph 로 돌려 기대 결과와 비교한다.

지표: 기대 verdict 일치율 · 인용 유효율 · 정상(pass 기대) 케이스 오탐 · LLM 호출 수·토큰·지연.
LLM 은 같은 입력에도 출력이 흔들리므로 실제 LLM 평가는 케이스마다 repeat 회 돌린다.

케이스 세 가지 (cases.yaml 머리말 참고):
- 명세 케이스: sample(+mutate) → run_graph
- intake 케이스: 명세가 없거나 깨진 PR — Review API 와 같은 순서(레포 분석 → 생성 또는 LLM 복구)로
  deploy.yaml 을 만든 뒤 run_graph. 거절되면 verdict 는 intake_rejected (/verify 의 intake_{status} 와 같은 뜻)
- resume 케이스: needs_human 뒤 사람 응답(값 없음·일부만·잘못된 경로)을 승인 API 와 같은 검사로 넣고 재개
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from review_ai.graph import check_edited_ops, initial_state, run_graph
from review_ai.intake import IntakeKind, IntakeOutcome, prepare_intake
from review_ai.intake.analyze import RepoAnalysis, analyze_repository
from review_ai.intake.fake_repair import REPAIR_FAKES
from review_ai.intake.repair import repair_intake
from review_ai.judge.fake_llm import FAKES
from review_ai.judge.llm import LlmClient, LlmRequest, LlmResponse
from review_ai.patching import apply_ops, parse_pointer
from review_ai.preparation import GenerationContext
from review_ai.recommendations import resolve_human_decision
from review_ai.retrieval import Retriever
from review_ai.spec.deploy_spec import DeploySpec, user_fields

AI_ROOT = Path(__file__).resolve().parent.parent
EVAL_CASES = AI_ROOT / "eval" / "cases.yaml"
REPOS = AI_ROOT / "eval" / "repos"
SAMPLES = AI_ROOT / "samples"
REVIEWER = "reviewer"
INTAKE_REJECTED = "intake_rejected"
# 복구 LLM 자리 — 정답 명세를 받아 LlmClient 를 만든다 (가짜 oracle 은 정답을, 실제 Claude 는 무시한다)
Repairer = Callable[[dict[str, Any]], LlmClient]


class RecordingLLM:
    """요청·응답을 기록한다. 프롬프트에 비밀 값이 들어갔는지, 몇 번 불렀는지 보는 데 쓴다."""

    def __init__(self, inner: LlmClient) -> None:
        self._inner = inner
        self.model = inner.model
        self.requests: list[LlmRequest] = []
        self.responses: list[LlmResponse] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
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
            value["spec"] = user_fields(spec)
    return apply_ops(spec, ops)


def load_repo(name: str) -> RepoAnalysis:
    """eval/repos/<name>.yaml → Review API 와 같은 레포 분석 결과 (context·근거)."""
    data = yaml.safe_load((REPOS / f"{name}.yaml").read_text(encoding="utf-8"))
    files: dict[str, str] = data["files"]
    return analyze_repository(GenerationContext.model_validate(data["context"]), [*files, *data.get("tree", [])], files)


def intake_text(case: dict[str, Any]) -> str | None:
    """PR 의 deploy.yaml 원문. None 이면 파일이 없다. sample 에 edit([찾을 글자, 바꿀 글자])을 차례로 적용한다."""
    intake = case["intake"]
    if intake.get("file") == "missing":
        return None
    if "raw" in intake:
        return intake["raw"]
    text = (SAMPLES / intake["sample"]).read_text(encoding="utf-8")
    for old, new in intake.get("edit") or []:
        if text.count(old) != 1:  # 샘플이 바뀌어 고칠 곳이 사라지면 케이스가 조용히 정상 명세를 돌리게 된다
            raise ValueError(f"{case['id']}: 고칠 글자가 샘플에 정확히 한 번 있어야 한다: {old!r}")
        text = text.replace(old, new)
    return text


def intake_kind(text: str | None) -> IntakeKind | None:
    """Review API load_spec 과 같은 분류. None 이면 그대로 검토할 수 있는 명세다."""
    if text is None:
        return "missing"
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return "yaml_error"
    if loaded is None or loaded == {}:
        return "empty"
    try:
        DeploySpec.model_validate(loaded)
    except ValidationError:
        return "schema_error"
    return None


def intake_answer(case: dict[str, Any]) -> dict[str, Any] | None:
    """복구 정답 — 고치기 전 샘플(raw 케이스엔 없다). 가짜 복구가 내고, preserves_sample 이 결과와 비교한다."""
    sample = case["intake"].get("sample")
    return yaml.safe_load((SAMPLES / sample).read_text(encoding="utf-8")) if sample else None


async def run_intake(case: dict[str, Any],
                     make_repair: Callable[[], RecordingLLM]) -> tuple[IntakeKind, IntakeOutcome, RecordingLLM | None]:
    """복구 LLM 은 원문이 형식 오류일 때만 만든다 — 생성 케이스엔 정답 샘플이 없어도 된다."""
    analysis = load_repo(case["intake"]["repo"])
    text = intake_text(case)
    kind = intake_kind(text)
    if kind is None:
        raise ValueError(f"{case['id']}: intake 케이스의 원문이 정상 명세다 — edit 가 깨뜨리지 못했다")
    if kind not in ("yaml_error", "schema_error"):
        return kind, prepare_intake(kind, context=analysis.context, findings=analysis.findings), None
    assert text is not None
    repair_llm = make_repair()
    outcome = await repair_intake(kind, text, context=analysis.context, baseline=None, llm=repair_llm)
    return kind, outcome, repair_llm


@dataclass
class _Run:
    """케이스 한 번 실행의 결과. spec·final 은 intake 가 거절되면 None 이다."""

    spec: dict[str, Any] | None
    final: dict[str, Any] | None
    outcome: IntakeOutcome | None = None
    kind: IntakeKind | None = None
    repair_llm: RecordingLLM | None = None
    paused: str | None = None       # resume 케이스: 사람 응답을 넣기 전 verdict (needs_human 이어야 한다)
    resume_error: str | None = None
    unchanged: bool = True  # 사람 응답이 거절됐을 때 상태가 그대로인가

    @property
    def verdict(self) -> str:
        return self.final["decision"]["verdict"] if self.final else INTAKE_REJECTED

    @property
    def reasons(self) -> list[str]:
        return self.final["decision"]["reasons"] if self.final else [self.outcome.reason] if self.outcome else []


async def resume(final: dict[str, Any], answer: dict[str, Any], llm: LlmClient,
                 retriever: Retriever) -> tuple[dict[str, Any], str | None, bool]:
    """승인 API 와 같은 검사(resolve_human_decision → check_edited_ops)를 거쳐 재개.

    거절되면 (원래 상태, 사유, 상태가 그대로인가) — 승인 API 는 이때 422 를 돌려주고 검토를 재개하지 않는다.
    """
    human = {"decision": "approved", "approver": "eval", "edited_ops": copy.deepcopy(answer.get("edited_ops") or []),
             "use_recommendations": answer.get("use_recommendations", True)}
    before = copy.deepcopy(final)
    try:
        resolved = resolve_human_decision(final, human)
        check_edited_ops(final["deploy_spec"], resolved["edited_ops"])
    except ValueError as exc:  # PatchError·ValidationError — 승인 API 는 422
        return final, str(exc), final == before
    resumed = await run_graph({**final, "human_decision": human}, llm=llm, retriever=retriever)
    return resumed, None, True


def _pointer_value(doc: Any, pointer: str) -> Any:
    for token in parse_pointer(pointer):
        doc = doc[int(token)] if isinstance(doc, list) else doc.get(token)
        if doc is None:
            return None
    return doc


def _effective(raw: Any) -> DeploySpec:
    """배포 결과로 비교할 명세 — namespace 생략은 metadata.name 과 같다 (overlay 렌더러와 같은 규칙)."""
    spec = DeploySpec.model_validate(raw)
    target = spec.target.model_copy(update={"namespace": spec.target.namespace or spec.metadata.name})
    return spec.model_copy(update={"target": target})


def _check_intake(case: dict[str, Any], run: _Run) -> list[str]:
    exp, outcome, failures = case["expect"], run.outcome, []
    if outcome is None:
        return failures
    if "intake_kind" in exp and run.kind != exp["intake_kind"]:
        failures.append(f"원문 분류 {run.kind} != {exp['intake_kind']} — edit 가 의도와 다르게 깨뜨렸다")
    if "intake" in exp and outcome.action != exp["intake"]:
        failures.append(f"intake {outcome.action}({outcome.reason}) != {exp['intake']}: {outcome.message}")
    codes = {d.get("code") for d in outcome.details}
    if set(exp.get("intake_issues", [])) - codes:
        failures.append(f"복구 게이트 사유 누락 {exp['intake_issues']} (실제 {sorted(c for c in codes if c)})")
    if exp.get("preserves_sample"):
        answer = intake_answer(case)
        if outcome.content is None or answer is None:
            failures.append("비교할 복구 결과나 정답 샘플이 없다")
        elif _effective(yaml.safe_load(outcome.content)) != _effective(answer):
            failures.append("복구한 명세가 원래 샘플과 다르다")
    return failures


def _check_resume(case: dict[str, Any], run: _Run) -> list[str]:
    exp, failures = case["expect"], []
    if run.paused not in (None, "needs_human"):  # 승인 API 는 needs_human 검토에만 응답을 받는다
        failures.append(f"사람 확인에서 멈추지 않았다({run.paused}) — 재개할 수 없는 상태에 응답을 넣었다")
    if "resume_error" in exp:
        if run.resume_error is None or exp["resume_error"] not in run.resume_error:
            failures.append(f"사람 응답 거절 사유 {run.resume_error!r} 에 {exp['resume_error']!r} 가 없다")
        if not run.unchanged:
            failures.append("거절된 사람 응답이 상태를 바꿨다")
    elif run.resume_error is not None:
        failures.append(f"사람 응답이 거절됐다: {run.resume_error}")
    for pointer, value in (exp.get("values") or {}).items():
        actual = _pointer_value(run.final["deploy_spec"], pointer) if run.final else None
        if actual != value:
            failures.append(f"{pointer} = {actual!r} != {value!r}")
    return failures


def _check(case: dict[str, Any], run: _Run, llms: Sequence[RecordingLLM]) -> list[str]:
    exp, llm = case["expect"], llms[0]
    failures = _check_intake(case, run) + _check_resume(case, run)
    for secret in exp.get("prompt_must_not_contain", []):
        if any(secret in r.user or secret in r.system for recorder in llms for r in recorder.requests):
            failures.append("프롬프트에 비밀 값이 들어갔다")
    if run.verdict != exp["verdict"]:
        failures.append(f"verdict {run.verdict} != {exp['verdict']} (사유 {run.reasons})")
    if run.final is None or run.spec is None:
        return failures
    final, spec, decision = run.final, run.spec, run.final["decision"]
    first_findings = final["rounds"][0]["finding_ids"] if final["rounds"] else [f["finding_id"] for f in final["findings"]]
    missing = set(exp.get("reasons_include", [])) - set(decision["reasons"])
    if missing:
        failures.append(f"사유 누락 {sorted(missing)} (실제 {decision['reasons']})")
    if "findings" in exp and sorted(fid.split(":")[0] for fid in first_findings) != sorted(exp["findings"]):
        failures.append(f"findings {first_findings} != {exp['findings']}")
    if "llm_calls" in exp and len(llm.requests) != exp["llm_calls"]:
        failures.append(f"LLM 호출 {len(llm.requests)} != {exp['llm_calls']}")
    if "rounds" in exp and len(final["rounds"]) != exp["rounds"]:
        failures.append(f"패치 회차 {len(final['rounds'])} != {exp['rounds']}")
    if exp.get("no_engine_change") and final["deploy_spec"]["database"]["engine"] != spec["database"]["engine"]:
        failures.append("DB 엔진이 바뀌었다")
    return failures


def _usage(*llms: RecordingLLM) -> dict[str, int]:
    total: dict[str, int] = {}
    for response in (r for llm in llms for r in llm.responses):
        for key, value in response.usage.items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total


def _repair_role(case: dict[str, Any]) -> str | None:
    intake = case.get("intake")
    return intake.get("repair", REVIEWER) if intake else None


def _repair_factory(case: dict[str, Any], repairer: Repairer | None) -> Callable[[], RecordingLLM]:
    role = _repair_role(case)

    def build() -> RecordingLLM:  # intake 케이스에서 원문이 형식 오류일 때만 불린다
        make = (repairer or REPAIR_FAKES["oracle"]) if role == REVIEWER else REPAIR_FAKES[str(role)]
        answer = intake_answer(case)
        if answer is None and make in REPAIR_FAKES.values():  # 실제 Claude 는 정답 없이 돈다
            raise ValueError(f"{case['id']}: 가짜 복구는 정답이 필요하다 — raw 대신 sample + edit 로 써라")
        return RecordingLLM(make(answer or {}))

    return build


async def _execute(case: dict[str, Any], llm: RecordingLLM, make_repair: Callable[[], RecordingLLM],
                   retriever: Retriever) -> _Run:
    outcome, kind, repair_llm = None, None, None
    if "intake" in case:
        kind, outcome, repair_llm = await run_intake(case, make_repair)
        if outcome.action == "rejected":
            return _Run(spec=None, final=None, outcome=outcome, kind=kind, repair_llm=repair_llm)
        assert outcome.content is not None
        spec = yaml.safe_load(outcome.content)
    else:
        spec = build_spec(case)
    final = await run_graph(initial_state(spec, review_id=f"eval-{case['id']}"), llm=llm, retriever=retriever)
    run = _Run(spec=spec, final=final, outcome=outcome, kind=kind, repair_llm=repair_llm)
    if "resume" in case:
        run.paused = final["decision"]["verdict"]
        run.final, run.resume_error, run.unchanged = await resume(final, case["resume"], llm, retriever)
    return run


async def run_case(case: dict[str, Any], reviewer: Callable[[], LlmClient], retriever: Retriever,
                   repairer: Repairer | None = None) -> CaseResult:
    """reviewer 는 judge 자리, repairer 는 형식 오류 복구 자리(intake 케이스). 없으면 가짜 oracle 복구."""
    role, repair_role = case["llm"], _repair_role(case)
    llm = RecordingLLM(reviewer() if role == REVIEWER else FAKES[role]())
    started = time.perf_counter()
    run = await _execute(case, llm, _repair_factory(case, repairer), retriever)
    validation = run.final["decision"]["validation"] if run.final else {}
    citations = validation.get("citations_ok") if validation.get("llm_available") else None
    recorders = (llm, run.repair_llm) if run.repair_llm else (llm,)
    failures = _check(case, run, recorders)
    # 일부러 틀린 복구 가짜를 쓰는 케이스는 게이트 평가다 — reviewer 지표에 섞지 않는다
    label = f"repair:{repair_role}" if repair_role not in (None, REVIEWER) else role
    return CaseResult(
        case_id=case["id"], llm_role=label, ok=not failures, failures=failures,
        verdict=run.verdict, reasons=run.reasons, llm_calls=sum(len(r.requests) for r in recorders),
        citations_ok=citations, usage=_usage(*recorders), seconds=time.perf_counter() - started,
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
