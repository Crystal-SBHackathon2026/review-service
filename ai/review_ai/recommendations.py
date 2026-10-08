"""사람 확인에 표시할 검증된 권장값. 사용자 입력이 없는 필드에만 적용한다."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any

from review_ai.patching import PatchError, apply_ops, parse_pointer, path_under
from review_ai.secrets_pattern import MASK
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.state import Finding, Patch
from review_ai.static_check import run_static_check


def _overlaps(a: str, b: str) -> bool:
    left, right = parse_pointer(a), parse_pointer(b)
    return left[:len(right)] == right or right[:len(left)] == left


def _check_recommended_path(doc: Any, path: str) -> None:
    """권장값 적용 후 실제 값이 있는 경로만 허용한다. 스칼라의 하위 경로는 없다."""
    node = doc
    for token in parse_pointer(path):
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif (isinstance(node, list) and token.isascii() and token.isdigit()
              and str(int(token)) == token and int(token) < len(node)):
            node = node[int(token)]
        else:
            raise PatchError(f"권장값에 없는 경로: {path}")


def build_recommendations(spec: dict[str, Any], findings: Sequence[Finding],
                          patch: Patch | None = None) -> list[dict[str, Any]]:
    """LLM 패치 게이트를 통과한 값과 규칙·baseline으로 확정할 수 있는 값만 제안한다.

    판단 시 적용하지 않는다. 승인 뒤에도 형식 검사와 정적 검사를 다시 거친다.
    시크릿·probe·빌드 플랫폼처럼 관측 없이 정할 수 없는 값은 만들지 않는다.
    """
    before = DeploySpec.model_validate(spec)
    candidates: list[dict[str, Any]] = []
    covered: set[str] = set()
    if patch:
        covered.update(patch["target_finding_ids"])
        candidates.append({"finding_ids": list(patch["target_finding_ids"]), "source": "validated_patch",
                           "why": "패치 검증을 통과한 수정값", "ops": copy.deepcopy(patch["ops"])})
    previous = before.baseline.spec if before.baseline else None
    if previous and (previous.metadata.name, previous.metadata.repository, previous.target) != (
        before.metadata.name, before.metadata.repository, before.target
    ):
        previous = None
    for finding in findings:
        if finding["finding_id"] in covered:
            continue
        rule, path = finding["rule_id"], finding["location"]["spec_path"]
        ops: list[dict[str, Any]] = []
        source, why = "rule", ""
        if rule == "DB-003":
            ops = [{"op": "add", "path": "/runtime/replicas", "value": 1}]
            why = "SQLite 파일 공유를 피하도록 replica를 1로 설정"
        elif rule == "STO-003":
            ops = [{"op": "replace", "path": path, "value": False}]
            why = "버킷 공개를 끄고 비공개로 유지"
        elif rule in {"DB-001", "DB-008"} and previous:
            old, current = previous.database, before.database
            for field in ("engine", "version", "placement", "volume"):
                if getattr(old, field) != getattr(current, field):
                    ops.append({"op": "add", "path": f"/database/{field}", "value": getattr(old, field)})
            source, why = "baseline", "데이터를 유지하도록 이전 승인 명세의 DB 설정을 유지"
        elif rule == "STO-005" and previous:
            index = int(parse_pointer(path)[2])
            current = before.storage.volumes[index]
            old_volume = next((v for v in previous.storage.volumes if v.name == current.name), None)
            if old_volume:
                ops = [{"op": "replace", "path": path, "value": old_volume.size}]
                source, why = "baseline", "볼륨을 축소하지 않고 이전 승인 명세의 크기를 유지"
        if ops:
            candidates.append({"finding_ids": [finding["finding_id"]], "source": source, "why": why, "ops": ops})

    accepted: list[dict[str, Any]] = []
    current_spec = copy.deepcopy(spec)
    initial_ids = {f["finding_id"] for f in findings}
    for candidate in candidates:
        try:
            changed = apply_ops(current_spec, candidate["ops"])
            after = DeploySpec.model_validate(changed)
            remaining = {f["finding_id"] for f in run_static_check(after)}
        except (ValueError, TypeError, KeyError, IndexError):
            continue
        # 대상 문제를 해결하고 새 문제를 만들지 않는 후보만 반환한다.
        if remaining & set(candidate["finding_ids"]) or not remaining <= initial_ids:
            continue
        if MASK in json.dumps(candidate["ops"], ensure_ascii=False):
            continue
        accepted.append(candidate)
        current_spec = changed
    return accepted


def resolve_human_decision(state: dict[str, Any], human: dict[str, Any]) -> dict[str, Any]:
    """미입력을 저장된 권장값으로 보충한다. false/0/null은 명시 입력으로 취급한다.

    전체 응답을 기다리는 타이머는 아니다. approved 응답이 왔을 때만 실행한다.
    use_recommendations=False 는 현재 명세를 그대로 승인하는 명시적 선택이다.
    입력도 권장값도 없으면 edited_ops=[] — 기존 계약대로 그대로 승인이다(권장값을 못 만드는 사유도 막히지 않게).
    """
    if human["decision"] != "approved":
        return copy.deepcopy(human)
    supplied: list[dict[str, Any]] = []
    unanswered: list[str] = []
    for op in human.get("edited_ops") or []:
        path = op["path"]
        if parse_pointer(path)[0] == "baseline":
            raise PatchError("baseline은 사람이 수정할 수 없다")
        if op["op"] != "remove" and ("value" not in op or isinstance(op["value"], str) and not op["value"].strip()):
            unanswered.append(path)
        else:
            supplied.append(copy.deepcopy(op))
    recommendations = (state.get("decision") or {}).get("recommendations") or []
    defaults = [copy.deepcopy(op) for rec in recommendations for op in rec["ops"]]
    if not human.get("use_recommendations", True):
        defaults = []
    # 생략 op를 버리기 전에 원래 경로를 검사한다. 아직 없는 선택 필드도 권장 add로 생기면 유효하다.
    recommended_spec = apply_ops(state["deploy_spec"], defaults) if unanswered else None
    for path in unanswered:
        if not any(_overlaps(path, op["path"]) for op in defaults):
            raise PatchError(f"입력하지 않은 항목의 권장값이 없다: {path}")
        _check_recommended_path(recommended_spec, path)
    # 객체 전체를 명시한 경우도 사용자 선택으로 취급하며, 권장값으로 다시 덮지 않는다.
    defaults = [op for op in defaults if not any(path_under(op["path"], given["path"]) for given in supplied)]
    ops = defaults + supplied
    for op in ops:
        if parse_pointer(op["path"])[0] == "baseline":
            raise PatchError("baseline은 수정할 수 없다")
    DeploySpec.model_validate(apply_ops(state["deploy_spec"], ops))
    return {**copy.deepcopy(human), "edited_ops": ops,
            "defaulted_ops": copy.deepcopy(human.get("defaulted_ops", defaults))}
