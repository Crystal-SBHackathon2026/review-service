"""검토 그래프 State 와 내부 타입 — 회의 제안서 1절 기준.

제안서와 달라진 점 (Slack 공유 대상)
- Finding.category 에 'runtime' 추가 (결정 #5 제안)
- Finding 에 title·irreversible 추가 — decide_verdict 가 규칙 목록을 다시 읽지 않도록
- Patch 는 deploy_spec 에 대한 JSON Patch(ops) 가 정본이고, files 는 ops 를 overlay 로 렌더링한 diff 다
- deploy_result 추가 — 커밋 단계(commit_overlay) 결과. 판정과 섞지 않으려고 status 와 따로 둔다
- status 에 rejected 추가, human_decision 추가 — needs_human 뒤 사람 승인·거절로 재개 (Slack 10/08 합의)
  승인 + edited_ops 없음 → commit_overlay, 승인 + edited_ops 있음 → static_check 부터 재검사, 거절 → status=rejected 로 종료
- status 에 running 추가 — initial_state 값. judge 가 verdict 로 바꾼다
- autofix_commit 추가 — 워커가 applied_ops 를 커밋한 SHA 를 다시 검토할 때 True. 또 fix 면 needs_human(LOOP_EXHAUSTED)
- generated_spec 추가 — intake 가 baseline 없이 만든 명세의 검토면 True. pass 여도 needs_human(GENERATED_SPEC_UNVERIFIED)
- unverified_paths 추가 — 그중 레포로 확인하지 못해 후보값으로 채운 경로. judge 가 승인 화면의 확인 항목(권장값)으로 만든다.
  전부 확인된 생성 명세는 generated_spec 자체가 False 로 온다 (Review API 가 spec_intakes.unverified_paths 로 정한다)
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, NotRequired, TypedDict

Category = Literal["database", "secret", "network", "storage", "runtime"]
Severity = Literal["high", "medium", "low"]
Verdict = Literal["pass", "fix", "needs_human"]
ReviewStatus = Literal["running", Verdict, "rejected"]  # 판정 전 running → verdict 값 그대로, 사람이 거절하면 rejected
TargetEnv = Literal["aws", "gcp", "local"]

REASON_CODES = (
    "CITATION_INVALID",
    "LOW_SCORE",
    "AUTOFIX_FORBIDDEN",
    "PATCH_OUT_OF_SCOPE",
    "PATCH_MISSING",
    "IRREVERSIBLE",
    "LLM_UNAVAILABLE",
    "LOOP_EXHAUSTED",
    "GENERATED_SPEC_UNVERIFIED",
)
# 사유 코드만으로는 사람이 무엇을 확인할지 모르는 경우의 설명 — Review API 가 reason_messages 로 같이 보여 준다
REASON_MESSAGES: dict[str, str] = {
    "GENERATED_SPEC_UNVERIFIED": "레포로 확인하지 못한 값을 후보값으로 채운 생성 명세 — 권장값 표의 '확인 필요' 항목"
                                 "(이미지·포트·DB·시크릿·저장소 등)이 맞는지 보고 승인·수정",
}


class Finding(TypedDict):
    finding_id: str  # rule_id + 위치 해시. 회차 간 비교 키
    rule_id: str
    category: Category
    severity: Severity  # 규칙 목록에서. LLM 이 바꾸지 못함
    autofix: Literal["allowed", "forbidden"]  # when_no_data 는 여기서 이미 확정됨
    irreversible: bool
    title: str
    location: dict[str, str]  # {spec_path: "/database/engine"}
    evidence: str  # 비밀 값을 가린 값


class Doc(TypedDict):
    chunk_id: str
    rule_id: str | None  # incidents·guides 는 None 가능
    doc_type: Literal["rule", "incident", "guide", "case"]  # case: 업무 DB review_cases (지난 검토의 판단)
    provider: Literal["aws", "gcp", "local", "any"]
    score: float
    match: Literal["exact_rule", "semantic"]
    source_uri: str
    text: str


class Decision(TypedDict):
    verdict: Verdict
    reasons: list[str]
    items: list[dict[str, Any]]  # {finding_id, cited_rule_ids, why, fix_kind}
    extra_opinions: list[str]  # findings 밖 LLM 의견. verdict 에 영향 없음
    validation: dict[str, bool]  # {schema_ok, citations_ok, patch_scope_ok}
    llm: dict[str, Any] | None  # {model, prompt_version, usage, input_hash}
    recommendations: NotRequired[list[dict[str, Any]]]  # {finding_ids, source, why, ops}; 미입력 보충용


class PatchOp(TypedDict, total=False):
    op: Literal["replace", "add", "remove"]
    path: str  # deploy_spec 기준 JSON Pointer
    value: Any


class Patch(TypedDict):
    kind: Literal["config", "env", "code"]
    ops: list[PatchOp]
    target_finding_ids: list[str]
    files: list[dict[str, str]]  # {path, diff} — ops 적용 전후 overlay 렌더링 차이


class DeployResult(TypedDict):
    """커밋 단계(commit_overlay, 배포 담당) 결과. verdict 가 pass 일 때만 쓰이고 판정에는 영향이 없다."""

    status: Literal["committed", "blocked"]
    commit_sha: str | None  # committed 일 때 gitops 커밋 SHA. 배포 이벤트 매칭 키는 워커가 저장하는 앱 PR 병합 SHA
    reason: str | None  # blocked 일 때 멈춘 이유 (렌더러 blocking 경고의 code·설명 등)


class HumanDecision(TypedDict):
    """needs_human 뒤 사람 결정. 승인 API 가 review.resumed 로 보내고, 워커가 같은 thread_id 로 재개할 때 넣는다."""

    decision: Literal["approved", "rejected"]
    approver: str
    # 사람이 고친 ops. 원본이 아니라 State 의 현재 deploy_spec(AI 가 이미 고친 회차가 반영된 명세) 기준이다.
    # 미입력은 decision.recommendations로 보충. 기존 명세 그대로 승인하려면 use_recommendations=False
    edited_ops: list[PatchOp]
    use_recommendations: NotRequired[bool]  # 기본 True. False = 기존 명세 그대로 승인
    defaulted_ops: NotRequired[list[PatchOp]]  # 서버가 실제 보충한 권장값, 회차 기록용


class ReviewState(TypedDict, total=False):
    review_id: str
    target_env: TargetEnv
    spec_ref: dict[str, str]  # {repository, commit, path}
    deploy_spec: dict[str, Any]  # 워커가 baseline 까지 채운 명세 (dict)
    findings: list[Finding]
    retrieved_docs: list[Doc]
    decision: Decision
    patch: Patch | None
    rounds: Annotated[list[dict[str, Any]], operator.add]  # 회차별 스냅샷
    retry_count: int
    status: ReviewStatus  # verdict 값, 사람이 거절하면 rejected. 커밋 결과는 섞지 않고 deploy_result 에 둔다
    human_decision: HumanDecision | None  # needs_human 뒤 재개할 때만 있다. 있으면 AI 가 다시 자동 수정하지 않는다
    autofix_commit: bool  # 검토 대상 커밋이 워커가 applied_ops 를 커밋한 것(봇 커밋)이면 True — 재수정 루프 방지
    generated_spec: bool  # intake 가 baseline 없이 만든 명세면 True — 사람이 확인하기 전에는 병합하지 않는다
    unverified_paths: list[str]  # 그중 후보값으로 채운 경로 (/image 등). 승인 화면의 확인 항목
    deploy_result: DeployResult | None  # commit_overlay 만 쓴다 (pass 가 아니면 없음)
