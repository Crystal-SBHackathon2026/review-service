"""사람 확인에 표시할 검증된 권장값. 사용자 입력이 없는 필드에만 적용한다."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any

from review_ai.patching import PatchError, apply_ops, parse_pointer, path_under
from review_ai.secrets_pattern import MASK
from review_ai.spec.deploy_spec import BASE_RESOURCES, PIPELINE_FIELDS, DeploySpec, Volume
from review_ai.state import Finding, Patch
from review_ai.static_check import run_static_check


# 겹치거나 넓은 리스트 항목을 지우는 규칙 → 권장 이유
DROP_ENTRY_RULES = {
    "SEC-003": "이름이 겹친 시크릿 항목을 지우고 첫 항목만 남김 (같은 Secret 키를 읽어 값은 그대로)",
    "NET-002": "내부 전용 진입점에서 전체 대역(0.0.0.0/0·::/0) 허용을 지움",
}
BASE_LIMITS = {"cpu_limit": BASE_RESOURCES.cpu_limit, "memory_limit": BASE_RESOURCES.memory_limit}


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


def _resource_limit_ops(spec: dict[str, Any], before: DeploySpec) -> list[dict[str, Any]]:
    """비어 있는 상한만 base 값으로 채운다. 원문에 resources 가 없으면 객체째 넣는다 (요청값은 기본값 그대로)."""
    missing = {field: value for field, value in BASE_LIMITS.items()
               if getattr(before.runtime.resources, field) is None}
    if "resources" not in spec["runtime"]:
        return [{"op": "add", "path": "/runtime/resources", "value": missing}]
    return [{"op": "add", "path": f"/runtime/resources/{field}", "value": value} for field, value in missing.items()]


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
    removed: list[tuple[str, Volume]] = []
    dropped: dict[str, list[tuple[str, str]]] = {}
    for finding in findings:
        if finding["finding_id"] in covered:
            continue
        rule, path = finding["rule_id"], finding["location"]["spec_path"]
        ops: list[dict[str, Any]] = []
        source, why = "rule", ""
        if rule == "DB-003":
            ops = [{"op": "add", "path": "/runtime/replicas", "value": 1}]
            why = "SQLite 파일 공유를 피하도록 replica를 1로 설정"
        elif rule == "STO-002":
            ops = [{"op": "add", "path": "/runtime/replicas", "value": 1}]
            why = "ReadWriteOnce 볼륨을 한 Pod만 쓰도록 replica를 1로 설정"
        elif rule == "STO-003":
            ops = [{"op": "replace", "path": path, "value": False}]
            why = "버킷 공개를 끄고 비공개로 유지"
        elif rule == "DB-006":
            ops = [{"op": "replace", "path": path, "value": False}]
            why = "관리형 DB를 인터넷에 공개하지 않음"
        elif rule == "DB-007":
            ops = [{"op": "replace", "path": path, "value": 1}]
            why = "관리형 DB 자동 백업을 켬 (보관 1일, Terraform 기본값)"
        elif rule == "STO-004":
            ops = [{"op": "replace", "path": path, "value": True}]
            why = "버킷 암호화를 켬"
        elif rule in DROP_ENTRY_RULES:
            dropped.setdefault(rule, []).append((finding["finding_id"], path))
        elif rule == "SEC-004":
            ops = [{"op": "remove", "path": path}]
            why = "쓰이지 않는 runtime.env 항목을 지우고 시크릿 값만 남김"
        elif rule == "RUN-005":
            ops = _resource_limit_ops(spec, before)
            why = "gitops base 와 같은 리소스 상한(250m / 128Mi)을 채움"
        elif rule == "RUN-008" and path.endswith("/change") and before.observed is not None:
            ops = [{"op": "replace", "path": path, "value": before.observed.schema_change}]
            why = "새 마이그레이션 SQL 판정에 맞춰 스키마 변경 종류를 고침 — 실행 시점·전략이 따라온다"
        elif rule == "RUN-007":
            ops = [{"op": "add", "path": "/rollout", "value": {**spec.get("rollout", {}), "strategy": "bluegreen"}}]
            why = "두 버전이 섞여 요청을 받지 않도록 미리보기 확인 뒤 한 번에 전환(bluegreen)"
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
        elif rule == "STO-006" and previous:
            removed.append((finding["finding_id"], previous.storage.volumes[int(parse_pointer(path)[4])]))
        if ops:
            candidates.append({"finding_ids": [finding["finding_id"]], "source": source, "why": why, "ops": ops})
    for rule, entries in dropped.items():
        # 리스트 항목을 지우는 op 는 뒤에서부터 — 앞을 먼저 지우면 뒤 항목의 인덱스가 밀린다
        paths = sorted((path for _, path in entries), key=lambda p: int(parse_pointer(p)[-1]), reverse=True)
        candidates.append({"finding_ids": [fid for fid, _ in entries], "source": "rule", "why": DROP_ENTRY_RULES[rule],
                           "ops": [{"op": "remove", "path": path} for path in paths]})
    if removed:
        # 이전 볼륨을 한 op 로 되살린다 — persistent 를 끈 것은 그 자리에서 바꾸고, 지운 것은 뒤에 붙인다.
        # storage·volumes 키가 없어도 되고, 여럿이어도 서로 덮지 않는다
        storage = spec.get("storage") or {}
        restore = {v.name: v.model_dump(mode="json") for _, v in removed}
        kept = [restore.pop(v["name"], v) for v in storage.get("volumes", [])]
        volumes = [*kept, *restore.values()]
        candidates.append({"finding_ids": [fid for fid, _ in removed], "source": "baseline",
                           "why": "데이터를 유지하도록 이전 승인 명세의 볼륨을 되살림",
                           "ops": [{"op": "add", "path": "/storage", "value": {**storage, "volumes": volumes}}]})

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
        if parse_pointer(path)[0] in PIPELINE_FIELDS:
            raise PatchError(f"{parse_pointer(path)[0]}은 사람이 수정할 수 없다")
        if op["op"] != "remove" and ("value" not in op or isinstance(op["value"], str) and not op["value"].strip()):
            unanswered.append(path)
        else:
            supplied.append(copy.deepcopy(op))
    both = sorted(set(unanswered) & {op["path"] for op in supplied})
    if both:
        raise PatchError(f"같은 경로를 값 있음·값 없음으로 함께 보냈다: {', '.join(both)}")
    recommendations = (state.get("decision") or {}).get("recommendations") or []
    defaults = [copy.deepcopy(op) for rec in recommendations for op in rec["ops"]]
    if not human.get("use_recommendations", True):
        defaults = []
    # 생략 op를 버리기 전에 원래 경로를 검사한다. 아직 없는 선택 필드도 권장 add로 생기면 유효하다.
    recommended_spec = apply_ops(state["deploy_spec"], defaults) if unanswered else None
    for path in unanswered:
        if not any(_overlaps(path, op["path"]) for op in defaults):
            raise PatchError(f"입력하지 않은 항목의 권장값이 없다: {path} — 값을 넣거나 이 항목을 빼고 보내라"
                             " (작성한 명세 그대로 승인: edited_ops 없이 use_recommendations: false)")
        _check_recommended_path(recommended_spec, path)
    # 객체 전체를 명시한 경우도 사용자 선택으로 취급하며, 권장값으로 다시 덮지 않는다.
    defaults = [op for op in defaults if not any(path_under(op["path"], given["path"]) for given in supplied)]
    ops = defaults + supplied
    for op in ops:
        if parse_pointer(op["path"])[0] in PIPELINE_FIELDS:
            raise PatchError(f"{parse_pointer(op['path'])[0]}은 수정할 수 없다")
    DeploySpec.model_validate(apply_ops(state["deploy_spec"], ops))
    return {**copy.deepcopy(human), "edited_ops": ops,
            "defaulted_ops": copy.deepcopy(human.get("defaulted_ops", defaults))}
