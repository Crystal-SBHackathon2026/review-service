"""검토 그래프 — 파이프라인 담당의 조립 함수가 나오기 전까지 AI 판단 쪽 단독 실행·평가셋용.

    START ─┬─────────────────────▶ static_check → retrieve_evidence → judge ─┬─ pass / needs_human → END
           └─ apply_human_edits ──▶ ▲                                        └─ fix → apply_patch ─┘(재검사)
              (승인 + edited_ops)

needs_human 뒤 재개는 멈춘 State 에 human_decision 을 넣어 다시 돌린다. 승인 + edited_ops 일 때만 이 그래프를 다시 타고,
승인만이면 commit_overlay, 거절이면 status=rejected — 둘 다 파이프라인 몫이라 여기엔 없다.

노드 모양(async, 바뀐 필드만 반환, 클라이언트는 팩토리로 주입)은 제안서 4절과 같다. 파이프라인 그래프로 바꿀 때
make_* 팩토리를 그대로 add_node 하면 된다. apply_patch·라우터는 파이프라인 소유이고 여기 것은 참고 구현이다.

apply_patch 는 patch 를 rounds 로 옮기고 비운다. 그래서 고쳐서 통과한 최종 State 는 verdict=pass·patch=None 이다.
커밋 단계는 patch 가 아니라 verdict.applied_ops(final) — run_graph 반환값의 applied_ops — 를 spec_ref 원본에 적용한다.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from review_ai.judge.llm import LlmClient
from review_ai.judge.node import make_judge
from review_ai.judge.validate import overlay_files
from review_ai.patching import apply_ops
from review_ai.retrieval import Retriever, make_retrieve_evidence
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.state import Patch, ReviewState
from review_ai.static_check import make_static_check
from review_ai.verdict import MAX_PATCH_ROUNDS, applied_ops, round_snapshot

RECURSION_LIMIT = 4 * (MAX_PATCH_ROUNDS + 2)


async def apply_patch(state: dict[str, Any]) -> dict[str, Any]:
    patch = state["patch"]
    return {
        "rounds": [round_snapshot(state)],
        "deploy_spec": apply_ops(state["deploy_spec"], patch["ops"]),
        "retry_count": state.get("retry_count", 0) + 1,
        "patch": None,
    }


async def apply_human_edits(state: dict[str, Any]) -> dict[str, Any]:
    """사람이 고친 ops 를 현재 명세에 적용하고, AI 가 멈춘 이유와 승인자를 rounds 에 남긴다 → static_check 로 재검사.

    rounds 에 넣으므로 verdict.applied_ops(state) 에 사람 ops 가 AI 회차 뒤에 순서대로 들어간다 (커밋 노드는 그대로 쓰면 된다).
    재검사가 findings·decision 을 덮어쓰기 때문에 needs_human 사유·설명은 이 회차 기록에만 남는다.
    ops 가 명세를 깨면 ValueError — 승인 API 가 재개 전에 같은 검사(check_edited_ops)로 422 를 돌려주는 게 좋다.
    """
    human = state.get("human_decision")
    if not human or human["decision"] != "approved" or not human["edited_ops"]:
        raise ValueError("apply_human_edits 는 승인 + edited_ops 일 때만 — 승인만이면 commit_overlay, 거절이면 rejected")
    edited = check_edited_ops(state["deploy_spec"], human["edited_ops"])
    before, after = DeploySpec.model_validate(state["deploy_spec"]), DeploySpec.model_validate(edited)
    patch = Patch(kind="config", ops=[dict(op) for op in human["edited_ops"]],  # type: ignore[misc]
                  target_finding_ids=[f["finding_id"] for f in state.get("findings") or []],
                  files=overlay_files(before, after))
    snapshot = {**round_snapshot(state), "patch": patch,
                "human": {"decision": human["decision"], "approver": human["approver"]}}
    return {
        "rounds": [snapshot],
        "deploy_spec": edited,
        "retry_count": state.get("retry_count", 0) + 1,
        "patch": None,
        "status": "running",
    }


def check_edited_ops(deploy_spec: dict[str, Any], ops: list[Any]) -> dict[str, Any]:
    """사람이 고친 ops 를 적용한 명세. 경로가 없거나 명세 형식을 깨면 ValueError(PatchError·ValidationError).

    승인 API 가 review.resumed 를 보내기 전에 불러 422 로 돌려주면, 워커가 재개 중에 실패하지 않는다.
    """
    edited = apply_ops(deploy_spec, ops)
    DeploySpec.model_validate(edited)
    return edited


def route_start(state: dict[str, Any]) -> str:
    human = state.get("human_decision")
    if human and human["decision"] == "approved" and human["edited_ops"]:
        return "apply_human_edits"
    return "static_check"


def route_after_judge(state: dict[str, Any]) -> str:
    verdict = state["decision"]["verdict"]
    if verdict == "fix" and state.get("retry_count", 0) < MAX_PATCH_ROUNDS:
        return "apply_patch"
    return END


def build_graph(llm: LlmClient | None, retriever: Retriever) -> Any:
    graph = StateGraph(ReviewState)
    graph.add_node("static_check", make_static_check())
    graph.add_node("retrieve_evidence", make_retrieve_evidence(retriever))
    graph.add_node("judge", make_judge(llm))
    graph.add_node("apply_patch", apply_patch)
    graph.add_node("apply_human_edits", apply_human_edits)
    graph.add_conditional_edges(START, route_start, ["apply_human_edits", "static_check"])
    graph.add_edge("apply_human_edits", "static_check")
    graph.add_edge("static_check", "retrieve_evidence")
    graph.add_edge("retrieve_evidence", "judge")
    graph.add_conditional_edges("judge", route_after_judge, ["apply_patch", END])
    graph.add_edge("apply_patch", "static_check")
    return graph.compile()


def initial_state(deploy_spec: dict[str, Any], *, review_id: str, spec_ref: dict[str, str] | None = None,
                  autofix_commit: bool = False) -> ReviewState:
    return ReviewState(
        review_id=review_id,
        target_env=deploy_spec["target"]["env"],
        spec_ref=spec_ref or {},
        deploy_spec=deploy_spec,
        findings=[],
        retrieved_docs=[],
        patch=None,
        rounds=[],
        retry_count=0,
        status="running",
        autofix_commit=autofix_commit,
    )


async def run_graph(state: ReviewState, *, llm: LlmClient | None, retriever: Retriever) -> dict[str, Any]:
    """최종 State + status(verdict) + applied_ops(원본에 적용할 ops) + patched(ops 가 하나라도 있는지).

    고쳐서 통과 = status == "pass" and patched. 수정 없이 통과면 applied_ops == [] 이다.
    """
    final = await build_graph(llm, retriever).ainvoke(state, {"recursion_limit": RECURSION_LIMIT})
    ops = applied_ops(final)
    return {**final, "applied_ops": ops, "patched": bool(ops)}  # status 는 judge 가 verdict 로 채운다
