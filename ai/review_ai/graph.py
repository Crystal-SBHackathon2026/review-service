"""검토 그래프 — 파이프라인 담당의 조립 함수가 나오기 전까지 AI 판단 쪽 단독 실행·평가셋용.

    static_check → retrieve_evidence → judge ─┬─ pass / needs_human → END
          ▲                                   └─ fix → apply_patch ─┘(재검사)

노드 모양(async, 바뀐 필드만 반환, 클라이언트는 팩토리로 주입)은 제안서 4절과 같다. 파이프라인 그래프로 바꿀 때
make_* 팩토리를 그대로 add_node 하면 된다. apply_patch·라우터는 파이프라인 소유이고 여기 것은 참고 구현이다.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from review_ai.judge.llm import LlmClient
from review_ai.judge.node import make_judge
from review_ai.patching import apply_ops
from review_ai.retrieval import Retriever, make_retrieve_evidence
from review_ai.state import ReviewState
from review_ai.static_check import make_static_check
from review_ai.verdict import MAX_PATCH_ROUNDS, round_snapshot

RECURSION_LIMIT = 4 * (MAX_PATCH_ROUNDS + 2)


async def apply_patch(state: dict[str, Any]) -> dict[str, Any]:
    patch = state["patch"]
    return {
        "rounds": [round_snapshot(state)],
        "deploy_spec": apply_ops(state["deploy_spec"], patch["ops"]),
        "retry_count": state.get("retry_count", 0) + 1,
        "patch": None,
    }


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
    graph.add_edge(START, "static_check")
    graph.add_edge("static_check", "retrieve_evidence")
    graph.add_edge("retrieve_evidence", "judge")
    graph.add_conditional_edges("judge", route_after_judge, ["apply_patch", END])
    graph.add_edge("apply_patch", "static_check")
    return graph.compile()


def initial_state(deploy_spec: dict[str, Any], *, review_id: str, spec_ref: dict[str, str] | None = None) -> ReviewState:
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
    )


async def run_graph(state: ReviewState, *, llm: LlmClient | None, retriever: Retriever) -> dict[str, Any]:
    final = await build_graph(llm, retriever).ainvoke(state, {"recursion_limit": RECURSION_LIMIT})
    return {**final, "status": final["decision"]["verdict"]}
