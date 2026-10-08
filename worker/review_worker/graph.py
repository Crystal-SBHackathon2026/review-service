"""워커 검토 그래프 — review_ai 판단 노드에 사람 확인·CI 대기·PR 병합·커밋 단계를 붙인다.

    static_check → retrieve_evidence → judge → record_result ─┬─ fix → apply_patch → static_check (최대 MAX_PATCH_ROUNDS)
                                                              ├─ needs_human → await_human → wait_human ⏸
                                                              │     승인 + edited_ops → apply_human_ops → static_check
                                                              │     승인            → await_ci
                                                              │     거절            → END (rejected)
                                                              └─ pass → await_ci → wait_ci ⏸
                                                                    success → merge_pr → commit_overlay → END
                                                                    그 밖    → END (failed)

⏸ 는 LangGraph interrupt. review.resumed 메시지가 오면 같은 thread_id(review_id) 로 Command(resume=...) 재개한다.
interrupt 노드는 재개 때 처음부터 다시 실행되므로, 상태 기록(await_*)과 대기(wait_*)를 다른 노드로 나눴다.
apply_patch·라우터는 review_ai/graph.py 참고 구현을 옮겨 왔다(파이프라인 소유).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from review_ai.errors import TransientError
from review_ai.judge.llm import LlmClient
from review_ai.judge.node import judge_unavailable, make_judge
from review_ai.patching import apply_ops
from review_ai.retrieval import Retriever, make_retrieve_evidence
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.state import DeployResult, ReviewState
from review_ai.static_check import make_static_check
from review_ai.verdict import MAX_PATCH_ROUNDS, round_snapshot
from review_common.github import GitHubError
from review_common.repository import ReviewRepository
from review_common.resumed import CiResult, HumanDecisionModel

log = logging.getLogger(__name__)

JUDGE_MAX_RETRIES = 3  # TransientError 재시도 횟수. 소진되면 judge_unavailable 로 계속한다
RECURSION_LIMIT = 100

CommitOverlay = Callable[[dict[str, Any], str], Awaitable[DeployResult]]


class PullRequestMerger(Protocol):
    async def pulls_for_commit(self, repository: str, sha: str) -> list[dict[str, Any]]: ...

    async def merge_pull(self, repository: str, number: int, *, head_sha: str) -> str: ...


async def commit_overlay_stub(state: dict[str, Any], merge_sha: str) -> DeployResult:
    """commit_overlay 자리 — 성진님 구현으로 바꾼다.

    입력: 최종 State(spec_ref·applied_ops 는 verdict.applied_ops(state) 로) 와 업무 DB 의 병합 SHA.
    출력: DeployResult. committed 면 commit_sha 에 gitops 커밋 SHA.
    """
    log.warning("review %s: commit_overlay 미구현 — blocked 로 끝낸다 (merge_sha=%s)", state.get("review_id"), merge_sha)
    return DeployResult(status="blocked", commit_sha=None, reason="COMMIT_OVERLAY_NOT_IMPLEMENTED")


@dataclass
class Deps:
    repo: ReviewRepository
    github: PullRequestMerger
    llm: LlmClient | None
    retriever: Retriever
    commit_overlay: CommitOverlay = commit_overlay_stub
    judge_max_retries: int = JUDGE_MAX_RETRIES
    retry_backoff_seconds: float = 1.0


def app_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """baseline 을 뺀 명세 — 업무 DB final_spec 과 다음 배포의 baseline.spec 으로 쓴다."""
    return {k: v for k, v in spec.items() if k != "baseline"}


def build_graph(deps: Deps, checkpointer: Any) -> Any:
    repo = deps.repo
    judge_once = make_judge(deps.llm)

    async def judge(state: dict[str, Any]) -> dict[str, Any]:
        last: TransientError | None = None
        for attempt in range(deps.judge_max_retries + 1):
            try:
                return await judge_once(state)
            except TransientError as exc:
                last = exc
                log.warning("review %s: judge 일시 오류 %d/%d — %s", state["review_id"], attempt + 1,
                            deps.judge_max_retries + 1, exc)
                if attempt < deps.judge_max_retries:
                    await asyncio.sleep(deps.retry_backoff_seconds * 2**attempt)
        return judge_unavailable(state, error=f"transient: {last}")

    async def record_result(state: dict[str, Any]) -> dict[str, Any]:
        decision = state["decision"]
        await repo.update_review(
            state["review_id"], verdict=decision["verdict"], reasons=list(decision["reasons"]), decision=decision,
            findings=state.get("findings") or [], rounds=state.get("rounds") or [],
            final_spec=app_spec(state["deploy_spec"]),
        )
        return {"status": decision["verdict"]}

    def route_after_result(state: dict[str, Any]) -> str:
        verdict = state["decision"]["verdict"]
        if verdict == "fix" and state.get("retry_count", 0) < MAX_PATCH_ROUNDS:
            return "apply_patch"
        if verdict == "pass":
            return "await_ci"
        return "await_human"  # needs_human. fix 인데 회차를 다 쓴 경우도 사람에게 (decide_verdict 가 막지만 안전장치)

    async def apply_patch(state: dict[str, Any]) -> dict[str, Any]:
        patch = state["patch"]
        return {
            "rounds": [round_snapshot(state)],
            "deploy_spec": apply_ops(state["deploy_spec"], patch["ops"]),
            "retry_count": state.get("retry_count", 0) + 1,
            "patch": None,
        }

    async def await_human(state: dict[str, Any]) -> dict[str, Any]:
        await repo.update_review(state["review_id"], status="needs_human")
        return {}

    async def wait_human(state: dict[str, Any]) -> Command:
        value = interrupt({"kind": "human_decision", "review_id": state["review_id"]})
        decision = HumanDecisionModel.model_validate(value).as_state()
        rid = state["review_id"]
        if decision["decision"] == "rejected":
            await repo.update_review(rid, status="rejected", human_decision=decision)
            return Command(goto=END, update={"human_decision": decision, "status": "rejected"})
        await repo.update_review(rid, human_decision=decision, error=None)
        goto = "apply_human_ops" if decision["edited_ops"] else "await_ci"
        return Command(goto=goto, update={"human_decision": decision})

    async def apply_human_ops(state: dict[str, Any]) -> Command:
        """사람이 고친 ops 를 rounds 에 회차로 남기고 적용한다 — applied_ops() 가 커밋 때 같이 이어 붙인다."""
        ops = state["human_decision"]["edited_ops"]
        try:
            edited = apply_ops(state["deploy_spec"], ops)
            DeploySpec.model_validate(edited)
        except Exception as exc:  # noqa: BLE001 — 잘못된 편집은 사람에게 다시 묻는다
            log.warning("review %s: 사람이 고친 ops 적용 실패 — %s", state["review_id"], exc)
            await repo.update_review(state["review_id"], error=f"edited_ops 적용 실패: {exc}"[:2000])
            return Command(goto="await_human", update={"human_decision": None})
        human_patch = {"kind": "config", "ops": ops, "target_finding_ids": [], "files": []}
        snapshot = round_snapshot({**state, "patch": human_patch})
        return Command(goto="static_check", update={"rounds": [snapshot], "deploy_spec": edited, "patch": None})

    async def await_ci(state: dict[str, Any]) -> dict[str, Any]:
        await repo.update_review(state["review_id"], status="waiting_ci", final_spec=app_spec(state["deploy_spec"]))
        return {}

    async def wait_ci(state: dict[str, Any]) -> Command:
        ci = CiResult.model_validate(interrupt({"kind": "ci_completed", "review_id": state["review_id"]}))
        if ci.conclusion != "success":
            await repo.update_review(state["review_id"], status="failed", error=f"CI {ci.conclusion} ({ci.head_sha})")
            return Command(goto=END)
        return Command(goto="merge_pr")

    async def merge_pr(state: dict[str, Any]) -> Command:
        rid, ref = state["review_id"], state["spec_ref"]
        repository, reviewed_sha = ref["repository"], ref["commit"]
        await repo.update_review(rid, status="merging")
        try:
            pulls = [p for p in await deps.github.pulls_for_commit(repository, reviewed_sha) if p.get("state") == "open"]
            if not pulls:
                raise GitHubError(f"{reviewed_sha} 를 포함한 열린 PR 이 없다")
            pull = pulls[0]
            if pull["head"]["sha"] != reviewed_sha:
                raise GitHubError(f"PR #{pull['number']} head {pull['head']['sha']} 가 검토한 {reviewed_sha} 와 다르다")
            merge_sha = await deps.github.merge_pull(repository, pull["number"], head_sha=reviewed_sha)
        except GitHubError as exc:
            log.warning("review %s: 병합 안 함 — %s", rid, exc)
            await repo.update_review(rid, status="failed", error=f"merge_pr: {exc}"[:2000])
            return Command(goto=END)
        await repo.update_review(rid, merge_sha=merge_sha)
        return Command(goto="commit_overlay")

    async def commit_overlay(state: dict[str, Any]) -> dict[str, Any]:
        row = await repo.get_review(state["review_id"])
        result = await deps.commit_overlay(state, row["merge_sha"])
        await repo.update_review(state["review_id"], status=result["status"], deploy_result=result,
                                 gitops_commit_sha=result.get("commit_sha"))
        return {"deploy_result": result}

    graph = StateGraph(ReviewState)
    graph.add_node("static_check", make_static_check())
    graph.add_node("retrieve_evidence", make_retrieve_evidence(deps.retriever))
    graph.add_node("judge", judge)
    graph.add_node("record_result", record_result)
    graph.add_node("apply_patch", apply_patch)
    graph.add_node("await_human", await_human)
    graph.add_node("wait_human", wait_human, destinations=("apply_human_ops", "await_ci", END))
    graph.add_node("apply_human_ops", apply_human_ops, destinations=("static_check", "await_human"))
    graph.add_node("await_ci", await_ci)
    graph.add_node("wait_ci", wait_ci, destinations=("merge_pr", END))
    graph.add_node("merge_pr", merge_pr, destinations=("commit_overlay", END))
    graph.add_node("commit_overlay", commit_overlay)

    graph.add_edge(START, "static_check")
    graph.add_edge("static_check", "retrieve_evidence")
    graph.add_edge("retrieve_evidence", "judge")
    graph.add_edge("judge", "record_result")
    graph.add_conditional_edges("record_result", route_after_result, ["apply_patch", "await_human", "await_ci"])
    graph.add_edge("apply_patch", "static_check")
    graph.add_edge("await_human", "wait_human")
    graph.add_edge("await_ci", "wait_ci")
    graph.add_edge("commit_overlay", END)
    return graph.compile(checkpointer=checkpointer)
