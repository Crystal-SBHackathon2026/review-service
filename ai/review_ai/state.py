"""검토 그래프 State 와 내부 타입 — 회의 제안서 1절 기준.

제안서와 달라진 점 (Slack 공유 대상)
- Finding.category 에 'runtime' 추가 (결정 #5 제안)
- Finding 에 title·irreversible 추가 — decide_verdict 가 규칙 목록을 다시 읽지 않도록
- Patch 는 deploy_spec 에 대한 JSON Patch(ops) 가 정본이고, files 는 ops 를 overlay 로 렌더링한 diff 다
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

Category = Literal["database", "secret", "network", "storage", "runtime"]
Severity = Literal["high", "medium", "low"]
Verdict = Literal["pass", "fix", "needs_human"]
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
    status: str
