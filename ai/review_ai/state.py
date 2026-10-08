"""검토 그래프 State 와 내부 타입 — 회의 제안서 1절 기준.

제안서와 달라진 점 (Slack 공유 대상)
- Finding.category 에 'runtime' 추가 (결정 #5 제안)
- Finding 에 title·irreversible 추가 — decide_verdict 가 규칙 목록을 다시 읽지 않도록
- Patch 는 deploy_spec 에 대한 JSON Patch(ops) 가 정본이고, files 는 ops 를 overlay 로 렌더링한 diff 다
- deploy_result 추가 — 커밋 단계(commit_overlay) 결과. 판정과 섞지 않으려고 status 와 따로 둔다
- status 에 rejected 추가, human_decision 추가 — needs_human 뒤 사람 승인·거절로 재개 (Slack 10/08 합의)
  승인 + edited_ops 없음 → commit_overlay, 승인 + edited_ops 있음 → static_check 부터 재검사, 거절 → status=rejected 로 종료
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

Category = Literal["database", "secret", "network", "storage", "runtime"]
Severity = Literal["high", "medium", "low"]
Verdict = Literal["pass", "fix", "needs_human"]
ReviewStatus = Literal[Verdict, "rejected"]  # verdict 값 그대로 + 사람이 거절한 경우
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
)


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
    doc_type: Literal["rule", "incident", "guide"]
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
    commit_sha: str | None  # committed 일 때 gitops 커밋 SHA — 배포 이벤트를 검토와 잇는 키
    reason: str | None  # blocked 일 때 멈춘 이유 (렌더러 blocking 경고의 code·설명 등)


class HumanDecision(TypedDict):
    """needs_human 뒤 사람 결정. 승인 API 가 review.resumed 로 보내고, 워커가 같은 thread_id 로 재개할 때 넣는다."""

    decision: Literal["approved", "rejected"]
    approver: str
    edited_ops: list[PatchOp]  # 사람이 고친 deploy_spec ops. 비어 있으면 고친 것 없이 승인


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
    human_decision: HumanDecision | None  # needs_human 뒤 재개할 때만 있다
    deploy_result: DeployResult | None  # commit_overlay 만 쓴다 (pass 가 아니면 없음)
