"""규칙 함수가 공유하는 입력과 Finding 생성."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from review_ai.catalog import Rule, TargetCaps
from review_ai.spec.deploy_spec import AppSpec, DeploySpec
from review_ai.state import Finding

SIZE_TO_MI = {"Mi": 1, "Gi": 1024, "Ti": 1024 * 1024}


@dataclass(frozen=True)
class Hit:
    """규칙 하나가 찾은 위치 하나. evidence 에는 비밀 값을 넣지 않는다."""

    spec_path: str
    evidence: str


@dataclass(frozen=True)
class CheckContext:
    spec: DeploySpec
    caps: TargetCaps

    @property
    def previous(self) -> AppSpec | None:
        return self.spec.baseline.spec if self.spec.baseline else None

    @property
    def has_existing_data(self) -> bool:
        """이전 배포에 DB 가 있었고 '데이터 없음'이 확인되지 않았으면 데이터가 있다고 본다."""
        baseline = self.spec.baseline
        if baseline is None or baseline.spec.database.engine == "none":
            return False
        return baseline.facts.database_has_data is not False


def size_mi(quantity: str) -> int:
    return int(quantity[:-2]) * SIZE_TO_MI[quantity[-2:]]


def finding_id(rule_id: str, spec_path: str) -> str:
    return f"{rule_id}:{hashlib.sha256(spec_path.encode()).hexdigest()[:8]}"


def make_finding(rule: Rule, hit: Hit, ctx: CheckContext) -> Finding:
    autofix = rule.autofix
    if autofix == "when_no_data":
        autofix = "forbidden" if ctx.has_existing_data else "allowed"
    return Finding(
        finding_id=finding_id(rule.id, hit.spec_path),
        rule_id=rule.id,
        category=rule.category,  # type: ignore[typeddict-item]
        severity=rule.severity,
        autofix=autofix,  # type: ignore[typeddict-item]
        irreversible=rule.irreversible,
        title=rule.title,
        location={"spec_path": hit.spec_path},
        evidence=hit.evidence,
    )
