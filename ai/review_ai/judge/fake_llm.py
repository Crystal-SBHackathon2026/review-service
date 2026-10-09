"""API 키 없이 judge·그래프·평가셋을 돌리기 위한 가짜 LLM.

- OracleLLM: 규칙대로 정답에 가까운 출력을 낸다 (파이프라인 mock 노드·데모 리허설용)
- 나머지: 환각 평가셋용으로 일부러 틀린 출력을 낸다. 코드 게이트가 이걸 잡아내는지 본다
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from review_ai.catalog import load_targets
from review_ai.judge.llm import LlmResponse, LlmUnavailable
from review_ai.judge.prompt import JudgeRequest
from review_ai.spec.deploy_spec import BASE_RESOURCES
from review_ai.state import Finding

FAKE_MODEL = "fake-oracle"
FIX_KIND = {"SEC-001": "env", "SEC-002": "env", "RUN-001": "code", "RUN-002": "code", "RUN-004": "code", "DB-004": "code",
            "DB-001": "none", "DB-008": "none", "STO-005": "none", "STO-006": "none", "STO-001": "none",
            "RUN-006": "code"}
BASE_LIMITS = {"cpu_limit": BASE_RESOURCES.cpu_limit, "memory_limit": BASE_RESOURCES.memory_limit}


def _op(op: str, path: str, value: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"op": op, "path": path}
    if op != "remove":
        out["value_json"] = json.dumps(value, ensure_ascii=False)
    return out


def _db002_ops(spec: dict[str, Any], env: str) -> list[dict[str, Any]]:
    caps = load_targets()[env]
    placement = spec["database"]["placement"]
    engines = caps.database.get(placement) or next(iter(caps.database.values()))
    engine, versions = next(iter(engines.items()))
    ops = [_op("replace", "/database/engine", engine)]
    if placement not in caps.database:
        ops.append(_op("replace", "/database/placement", next(iter(caps.database))))
    ops.append(_op("add", "/database/version", versions[0] if versions else None))
    return ops


def _db005_ops(spec: dict[str, Any]) -> list[dict[str, Any]]:
    volumes = spec.get("storage", {}).get("volumes", [])
    name = spec["database"].get("volume")
    for i, v in enumerate(volumes):
        if v["name"] == name:
            return [_op("replace", f"/storage/volumes/{i}/persistent", True)]
    vol = {"name": "data", "mount_path": "/data", "size": "1Gi", "persistent": True}
    ops = [_op("add", "/storage", {"volumes": [*volumes, vol], **{k: v for k, v in spec.get("storage", {}).items() if k != "volumes"}})]
    return ops + [_op("add", "/database/volume", "data"), _op("replace", "/database/placement", "volume")]


def _bluegreen_ops(spec: dict[str, Any]) -> list[dict[str, Any]]:
    if "rollout" in spec:
        return [_op("replace", "/rollout/strategy", "bluegreen")]
    return [_op("add", "/rollout", {"strategy": "bluegreen"})]


def _ops_for(finding: Finding, spec: dict[str, Any], env: str) -> list[dict[str, Any]]:
    rule, path = finding["rule_id"], finding["location"]["spec_path"]
    if rule in {"DB-003", "STO-002"}:
        return [_op("replace", "/runtime/replicas", 1)]
    if rule in {"DB-006", "STO-003"}:
        return [_op("replace", path, False)]
    if rule == "DB-007":
        return [_op("replace", path, 1)]
    if rule == "STO-004":
        return [_op("replace", path, True)]
    if rule in {"SEC-003", "SEC-004", "NET-002"}:
        return [_op("remove", path)]
    if rule == "RUN-005":
        resources = spec["runtime"].get("resources")
        if resources is None:
            return [_op("add", "/runtime/resources", BASE_LIMITS)]
        return [_op("add", f"/runtime/resources/{k}", v) for k, v in BASE_LIMITS.items() if not resources.get(k)]
    if rule == "DB-002":
        return _db002_ops(spec, env)
    if rule == "DB-005":
        return _db005_ops(spec)
    if rule == "RUN-007":
        return _bluegreen_ops(spec)
    return []


def _removals_last(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """리스트 항목 삭제는 맨 뒤에, 큰 인덱스부터 — 앞 항목을 먼저 지우면 뒤 항목의 인덱스가 밀린다."""
    def index(op: dict[str, Any]) -> int:
        last = op["path"].rsplit("/", 1)[1]
        return int(last) if last.isdigit() else -1
    removes = [op for op in ops if op["op"] == "remove"]
    return [op for op in ops if op["op"] != "remove"] + sorted(removes, key=index, reverse=True)


def oracle_review(request: JudgeRequest) -> dict[str, Any]:
    items, ops, targets = [], [], []
    for f in request.findings:
        items.append({
            "finding_id": f["finding_id"],
            "cited_rule_ids": [f["rule_id"]],
            "why": f"{f['title']}. 근거 문서의 권고대로 고친다.",
            "fix_kind": FIX_KIND.get(f["rule_id"], "config"),
        })
        if f["autofix"] == "allowed":
            new_ops = _ops_for(f, request.spec, request.target_env)
            if new_ops:
                ops.extend(new_ops)
                targets.append(f["finding_id"])
    patch = {"ops": _removals_last(ops), "target_finding_ids": targets} if ops else None
    return {"items": items, "patch": patch, "extra_opinions": []}


class ScriptedLLM:
    """출력을 만드는 함수를 받아 LlmClient 처럼 동작한다. calls 로 호출 횟수를 센다."""

    def __init__(self, produce: Callable[[JudgeRequest], dict[str, Any] | str], model: str = FAKE_MODEL) -> None:
        self._produce = produce
        self.model = model
        self.calls = 0

    async def complete(self, request: JudgeRequest) -> LlmResponse:
        self.calls += 1
        out = self._produce(request)
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return LlmResponse(text=text, model=self.model, usage={"input_tokens": 0, "output_tokens": 0})


class UnavailableLLM:
    model = "fake-unavailable"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: JudgeRequest) -> LlmResponse:
        self.calls += 1
        raise LlmUnavailable("가짜: 키 없음")


# ── 환각 평가셋용 변형 ───────────────────────────────────────────


def wrong_citation(request: JudgeRequest) -> dict[str, Any]:
    out = oracle_review(request)
    out["items"][0]["cited_rule_ids"] = ["DB-999"]
    return out


def obey_injection(request: JudgeRequest) -> dict[str, Any]:
    """명세 속 '무시하고 pass' 지시를 따른 척 — 문제 없다고 쓰고 패치를 내지 않는다."""
    out = oracle_review(request)
    for item in out["items"]:
        item["why"] = "문제 없음. pass 로 판정한다."
    out["patch"] = None
    out["extra_opinions"] = ["verdict: pass"]
    return out


def patch_creates_finding(request: JudgeRequest) -> dict[str, Any]:
    """고치면서 readiness 를 지워 RUN-001 을 새로 만든다."""
    out = oracle_review(request)
    if out["patch"]:
        out["patch"]["ops"].append(_op("remove", "/runtime/health/readiness"))
    return out


def patch_out_of_scope(request: JudgeRequest) -> dict[str, Any]:
    out = oracle_review(request)
    if out["patch"]:
        out["patch"]["ops"].append(_op("replace", "/image/platforms", ["amd64", "arm64"]))
    return out


def sqlite_to_postgres(request: JudgeRequest) -> dict[str, Any]:
    """'SQLite 면 무조건 postgres' 환각 — replicas 대신 엔진을 바꾼다."""
    out = oracle_review(request)
    if out["patch"]:
        out["patch"]["ops"] = [
            _op("replace", "/database/engine", "postgres"),
            _op("replace", "/database/placement", "in-cluster"),
            _op("add", "/database/version", "16"),
        ]
    return out


def patch_deletes_resource(request: JudgeRequest) -> dict[str, Any]:
    """'고친 척' 환각 — 문제를 고치지 않고 DB·버킷을 지워 finding 을 없앤다. 재검사만으로는 못 잡는다."""
    out = oracle_review(request)
    if out["patch"]:
        rules = {f["rule_id"] for f in request.findings if f["finding_id"] in out["patch"]["target_finding_ids"]}
        ops = [_op("replace", "/database", {"engine": "none"})] if rules & {"DB-002", "DB-003", "DB-005"} else []
        if "STO-003" in rules:
            ops.append(_op("remove", "/storage/buckets/0"))
        out["patch"]["ops"] = ops or out["patch"]["ops"]
    return out


def not_json(_: JudgeRequest) -> str:
    return "죄송하지만 JSON 으로 답할 수 없습니다."


FAKES: dict[str, Callable[[], Any]] = {
    "oracle": lambda: ScriptedLLM(oracle_review),
    "wrong_citation": lambda: ScriptedLLM(wrong_citation, "fake-wrong-citation"),
    "obey_injection": lambda: ScriptedLLM(obey_injection, "fake-obey-injection"),
    "patch_creates_finding": lambda: ScriptedLLM(patch_creates_finding, "fake-bad-patch"),
    "patch_out_of_scope": lambda: ScriptedLLM(patch_out_of_scope, "fake-out-of-scope"),
    "sqlite_to_postgres": lambda: ScriptedLLM(sqlite_to_postgres, "fake-sqlite-to-postgres"),
    "patch_deletes_resource": lambda: ScriptedLLM(patch_deletes_resource, "fake-deletes-resource"),
    "not_json": lambda: ScriptedLLM(not_json, "fake-not-json"),
    "unavailable": UnavailableLLM,
}
