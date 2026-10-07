"""static_check — deploy_spec 을 규칙 목록으로 검사한다. LLM 을 쓰지 않으며 같은 입력이면 같은 결과다."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from review_ai.catalog import load_rules, load_targets
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.state import Finding
from review_ai.static_check.context import CheckContext, make_finding
from review_ai.static_check.rules import CHECKS

Node = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def run_static_check(spec: DeploySpec) -> list[Finding]:
    rules = load_rules()
    ctx = CheckContext(spec=spec, caps=load_targets()[spec.target.env])
    findings = [
        make_finding(rules[rule_id], hit, ctx)
        for rule_id, check in CHECKS.items()
        if rules[rule_id].applies_to(spec.target.env)
        for hit in check(ctx)
    ]
    return sorted(findings, key=lambda f: (f["rule_id"], f["location"]["spec_path"]))


def make_static_check() -> Node:
    async def static_check(state: dict[str, Any]) -> dict[str, Any]:
        # 형식 오류는 Review API 가 422 로 막는다 — 여기서 실패하면 버그이므로 그대로 raise
        spec = DeploySpec.model_validate(state["deploy_spec"])
        return {"findings": run_static_check(spec)}

    return static_check
