"""워커 검토 그래프 — review_ai 판단 노드에 사람 확인·AI 수정 커밋·CI 확인·PR 병합·커밋 단계를 붙인다.

    START → static_check → retrieve_evidence → judge → record_result ─┬ (verdict 로 분기)
                 ▲
    fix               → apply_patch ─────────────────────────────────▶ static_check (최대 MAX_PATCH_ROUNDS)
    needs_human       → await_human → wait_human ⏸
                          승인 + edited_ops → apply_human_edits ──────▶ static_check
                          승인              → await_ci (AI 가 고친 회차가 있으면 commit_fix)
                          거절              → END (rejected)
    pass + 수정 있음  → commit_fix → END (superseded — 수정 커밋을 autofix_commit 새 검토로)
    pass + 수정 없음  → await_ci → check_ci ─ CI 끝남 → confirm_ci → guard_overlay → merge_pr → commit_overlay → END
                                           └ 아직    → wait_ci ⏸ → (재개) 다시 확인, 다른 suite 가 남았으면 await_ci

⏸ 는 LangGraph interrupt. review.resumed 가 오면 같은 thread_id(review_id) 로 Command(resume=...) 재개한다.
interrupt 노드는 재개 때 처음부터 다시 실행되므로 상태 기록(await_*)과 대기(wait_*)를 다른 노드로 나눴다.

멈춘 검토는 Review API 의 review sweep 이 회수한다 (review_worker.handler). 병합 뒤 gitops 커밋이 실패한 검토는
review.resumed(retry_overlay) 로 Command(goto="commit_overlay") — commit_overlay 만 다시 한다. 같은 내용이면
GitHubGitClient 가 새 커밋을 만들지 않으니 두 번 와도 된다.

CI 를 기다리기 전에 이미 끝났는지 먼저 본다 (gitops#9). 검토가 CI 보다 늦게 끝나면 check_suite 웹훅은 이미 지나가 있다.
check_ci 는 DB 를 waiting_ci 로 바꾼 **뒤에** 조회하므로, 그 뒤에 끝난 CI 는 웹훅이 waiting_ci 검토를 찾아 재개한다.
CI 결론은 언제나 GitHub check-suites 조회로 정한다 — 웹훅 한 건의 conclusion 으로는 병합하지 않는다 (P1-5).
조회가 실패하면 waiting_ci 로 남아 다음 check_suite 웹훅(또는 review sweep 의 재확인)을 기다린다.
confirm_ci 는 병합 직전에 전체 suite 를 한 번 더 본다 — 앱 레포마다 브랜치 보호 필수 체크가 다를 수 있다.

고쳐서 통과하면(applied_ops 가 있으면) 원본 deploy.yaml 에 ops 를 적용해 PR 브랜치에 커밋하고, 그 커밋 SHA 를
autofix_commit=True 새 검토로 넘긴다. 새 커밋이라 CI 가 다시 돌고, 앱 레포와 gitops 가 같은 명세를 갖는다.
새 검토에서 또 fix 가 나오면 judge 가 needs_human(LOOP_EXHAUSTED) 으로 막는다 (review_ai PR #11).

종료 지점(거절·commit_fix·사람 승인 뒤 committed)에서 판단 사례를 review_cases 에 남긴다 (review_ai.cases).
다음 검토의 retrieve_evidence 가 같은 규칙의 사례를 근거로 붙인다. 기록 실패는 검토 결과를 바꾸지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import yaml
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from review_ai.cases import case_from_state
from review_ai.deployment_analysis import case_advice
from review_ai.errors import TransientError
from review_ai.graph import apply_human_edits as ai_apply_human_edits
from review_ai.graph import apply_patch
from review_ai.judge.llm import LlmClient
from review_ai.judge.node import judge_unavailable, make_judge, spec_unverified
from review_ai.messages import TOPIC as REQUESTED_TOPIC
from review_ai.messages import build_review_requested
from review_ai.patching import apply_ops
from review_ai.recommendations import resolve_human_decision
from review_ai.retrieval import Retriever, make_retrieve_evidence
from review_ai.spec.deploy_spec import DeploySpec, user_fields
from review_ai.state import DeployResult, ReviewState
from review_ai.static_check import make_static_check
from review_ai.verdict import MAX_PATCH_ROUNDS, applied_ops
from review_common.github import GitHubError
from review_common.ids import new_review_id
from review_common.repository import ReviewRepository
from review_common.resumed import CiResult, HumanDecisionModel
from review_worker import metrics
from review_worker.metrics import app_env, timed_node

log = logging.getLogger(__name__)

JUDGE_MAX_RETRIES = 3  # TransientError 재시도 횟수. 소진되면 judge_unavailable 로 계속한다
GITHUB_MAX_ATTEMPTS = 3
RECURSION_LIMIT = 100
VERIFY_CONTEXT = "review-service/verify"  # 검토 결과 커밋 상태. sample-app 브랜치 보호의 필수 체크로 건다
VERIFY_MAX_ATTEMPTS = 3  # merge_pr 직전 success 쓰기 — 필수 체크라 못 쓰면 병합이 막힌다
CI_OK = frozenset({"success", "neutral", "skipped"})

COMMIT_OVERLAY_MAX_ATTEMPTS = 3  # commit_overlay 의 TransientError(네트워크·gitops 충돌) 재시도

# review_worker.commit_overlay.make_commit_overlay(git) 가 돌려주는 노드 모양. 바뀐 필드 {"deploy_result": ...} 만 돌려준다
CommitOverlay = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


# review_worker.commit_overlay.make_overlay_guard(git) — 병합하면 안 되는 사유(지금 배포 중인 ingress 삭제 등) 또는 None
OverlayGuard = Callable[[dict[str, Any]], Awaitable[str | None]]


async def no_overlay_guard(state: dict[str, Any]) -> str | None:
    """테스트용 — 검사하지 않는다. 실제 워커는 make_overlay_guard(GitHubGitClient) 를 쓴다."""
    return None


class GitHubPort(Protocol):
    async def get_file(self, repository: str, path: str, ref: str) -> str: ...

    async def prepare_file_commit(self, repository: str, *, parent: str, path: str, content: str,
                                  message: str) -> str: ...

    async def update_branch(self, repository: str, branch: str, sha: str) -> None: ...

    async def check_suites(self, repository: str, sha: str) -> list[dict[str, Any]]: ...

    async def pulls_for_commit(self, repository: str, sha: str) -> list[dict[str, Any]]: ...

    async def merge_pull(self, repository: str, number: int, *, head_sha: str) -> str: ...

    async def create_commit_status(self, repository: str, sha: str, *, state: str, context: str, description: str,
                                   target_url: str | None = None) -> None: ...


class Publisher(Protocol):
    async def send(self, topic: str, key: str, value: bytes) -> None: ...


async def commit_overlay_stub(state: dict[str, Any]) -> dict[str, Any]:
    """테스트용 — gitops 에 커밋하지 않고 blocked 로 끝낸다. 실제 워커는 make_commit_overlay(GitHubGitClient) 를 쓴다."""
    return {"deploy_result": DeployResult(status="blocked", commit_sha=None, reason="COMMIT_OVERLAY_NOT_IMPLEMENTED")}


@dataclass
class Deps:
    repo: ReviewRepository
    github: GitHubPort
    publisher: Publisher
    llm: LlmClient | None
    retriever: Retriever
    commit_overlay: CommitOverlay = commit_overlay_stub
    overlay_guard: OverlayGuard = no_overlay_guard
    ci_app_slug: str | None = "github-actions"  # 이 GitHub App 의 check suite 만 CI 로 본다. None 이면 전부
    public_url: str | None = None  # REVIEW_API_PUBLIC_URL — 커밋 상태 링크 /ui/reviews/{id}(진행 화면)의 앞부분
    judge_max_retries: int = JUDGE_MAX_RETRIES
    retry_backoff_seconds: float = 1.0


def is_fork_pull(pull: dict[str, Any], repository: str) -> bool:
    """PR head 가 이 레포가 아니거나(포크) head 레포가 없으면(포크가 지워짐) True."""
    head_repo = (pull.get("head") or {}).get("repo")
    return head_repo is None or head_repo.get("full_name") != repository


def app_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """파이프라인 필드(baseline·observed)를 뺀 명세 — 업무 DB final_spec 과 다음 배포의 baseline.spec 으로 쓴다."""
    return user_fields(spec)


def ci_conclusion(suites: list[dict[str, Any]], app_slug: str | None) -> str | None:
    """CI 결론. 해당 앱 suite 가 없거나 하나라도 안 끝났으면 None(기다린다). 전부 끝났으면 success 또는 첫 실패 결론."""
    mine = [s for s in suites if not app_slug or (s.get("app") or {}).get("slug") == app_slug]
    if not mine or any(s.get("status") != "completed" for s in mine):
        return None
    failed = [s.get("conclusion") or "unknown" for s in mine if s.get("conclusion") not in CI_OK]
    return failed[0] if failed else "success"


def build_graph(deps: Deps, checkpointer: Any) -> Any:
    repo, github = deps.repo, deps.github
    judge_once = make_judge(deps.llm)

    async def _retry(what: str, call: Callable[[], Awaitable[Any]]) -> Any:
        for attempt in range(GITHUB_MAX_ATTEMPTS):
            try:
                return await call()
            except GitHubError as exc:
                if attempt == GITHUB_MAX_ATTEMPTS - 1 or (exc.status_code and exc.status_code < 500):
                    raise
                log.warning("%s 재시도 %d — %s", what, attempt + 1, exc)
                await asyncio.sleep(deps.retry_backoff_seconds * 2**attempt)
        raise AssertionError("unreachable")

    async def _open_pull(ref: dict[str, str]) -> dict[str, Any]:
        """검토한 커밋이 아직 head 인 열린 PR. 없거나 head 가 움직였으면 GitHubError."""
        sha = ref["commit"]
        pulls = await _retry("PR 조회", lambda: github.pulls_for_commit(ref["repository"], sha))
        pulls = [p for p in pulls if p.get("state") == "open"]
        if not pulls:
            raise GitHubError(f"{sha} 를 포함한 열린 PR 이 없다")
        pull = pulls[0]
        if pull["head"]["sha"] != sha:
            raise GitHubError(f"PR #{pull['number']} head {pull['head']['sha']} 가 검토한 {sha} 와 다르다")
        return pull

    async def _verify_status(review_id: str, spec_ref: dict[str, str] | None, state: str, description: str, *,
                             attempts: int = 1) -> bool:
        """PR head 에 커밋 상태 review-service/verify. 실패해도 검토는 계속한다 (경고만). 썼으면 True."""
        if not spec_ref:
            return False
        url = f"{deps.public_url.rstrip('/')}/ui/reviews/{review_id}" if deps.public_url else None
        for attempt in range(1, attempts + 1):
            try:
                await github.create_commit_status(spec_ref["repository"], spec_ref["commit"], state=state,
                                                  context=VERIFY_CONTEXT, description=description, target_url=url)
                return True
            except Exception as exc:  # noqa: BLE001 — 권한·네트워크. 상태 표시 때문에 검토를 멈추지 않는다
                log.warning("review %s: 커밋 상태(%s) 기록 실패 %d/%d — %s", review_id, state, attempt, attempts, exc)
                if attempt < attempts:
                    await asyncio.sleep(deps.retry_backoff_seconds * 2**attempt)
        return False

    async def _fail(review_id: str, error: str) -> Command:
        log.warning("review %s: failed — %s", review_id, error)
        await repo.update_review(review_id, status="failed", error=error[:2000])
        row = await repo.get_review(review_id)
        if row:
            metrics.finished(row["app"], row["target_env"], "failed")
        await _verify_status(review_id, row and row.get("spec_ref"), "failure", error)
        return Command(goto=END)

    async def _record_case(state: dict[str, Any], *, human: dict[str, Any] | None = None) -> None:
        """끝난 검토를 판단 사례로 남긴다. 실패해도 검토는 그대로 끝낸다 — 사례는 다음 검토의 보조 근거다."""
        try:
            case = case_from_state(state, human=human)
            if case is not None:
                await repo.insert_case(**case)
        except Exception:
            log.warning("review %s: 판단 사례 기록 실패", state["review_id"], exc_info=True)

    # --- 판단 -------------------------------------------------------------------------------------

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

    async def inspect_failure_cases(state):
        advice = await case_advice(repo, state["deploy_spec"], state["spec_ref"].get("repository", ""))
        await repo.update_review(state["review_id"], case_advice=advice)
        return {}

    async def record_result(state: dict[str, Any]) -> dict[str, Any]:
        decision = state["decision"]
        await repo.update_review(
            state["review_id"], verdict=decision["verdict"], reasons=list(decision["reasons"]), decision=decision,
            findings=state.get("findings") or [], rounds=state.get("rounds") or [],
            final_spec=app_spec(state["deploy_spec"]), judged_at=datetime.now(UTC),
        )
        app, env = app_env(state)
        metrics.safe(lambda: metrics.VERDICTS.labels(app, env, decision["verdict"]).inc())
        if decision["verdict"] == "needs_human":
            for reason in decision["reasons"]:
                metrics.safe(lambda reason=reason: metrics.NEEDS_HUMAN_REASONS.labels(reason).inc())
        return {}

    def route_after_result(state: dict[str, Any]) -> str:
        verdict = state["decision"]["verdict"]
        if verdict == "fix" and state.get("retry_count", 0) < MAX_PATCH_ROUNDS:
            return "apply_patch"
        if verdict == "pass":
            return "commit_fix" if applied_ops(state) else "await_ci"
        return "await_human"  # needs_human. fix 인데 회차를 다 쓴 경우도 사람에게 (decide_verdict 가 막지만 안전장치)

    # --- 사람 확인 --------------------------------------------------------------------------------

    async def await_human(state: dict[str, Any]) -> dict[str, Any]:
        await repo.update_review(state["review_id"], status="needs_human")
        reasons = ", ".join((state.get("decision") or {}).get("reasons") or []) or "사유 없음"
        await _verify_status(state["review_id"], state.get("spec_ref"), "failure", f"사람 확인 필요: {reasons}")
        return {}

    async def wait_human(state: dict[str, Any]) -> Command:
        value = interrupt({"kind": "human_decision", "review_id": state["review_id"]})
        decision = HumanDecisionModel.model_validate(value).as_state()
        rid = state["review_id"]
        if decision["decision"] == "rejected":
            await repo.update_review(rid, status="rejected", human_decision=decision, human_decided_at=datetime.now(UTC))
            await _verify_status(rid, state.get("spec_ref"), "failure", f"사람이 거절 ({decision['approver']})")
            await _record_case(state, human=decision)
            metrics.finished(*app_env(state), "rejected")
            return Command(goto=END, update={"human_decision": decision, "status": "rejected"})
        try:
            decision = resolve_human_decision(state, decision)
        except ValueError as exc:
            await repo.update_review(rid, error=f"사람 응답 적용 실패: {exc}"[:2000])
            return Command(goto="await_human", update={"human_decision": None})
        await repo.update_review(rid, human_decision=decision, error=None, human_decided_at=datetime.now(UTC))
        if decision["edited_ops"]:
            goto = "apply_human_edits"
        elif applied_ops(state):  # 사람 확인 전에 AI 가 고친 회차가 있다 — 앱 레포에도 커밋해야 gitops 와 어긋나지 않는다
            goto = "commit_fix"
        else:
            goto = "await_ci"
        return Command(goto=goto, update={"human_decision": decision})

    async def apply_human_edits(state: dict[str, Any]) -> Command:
        """review_ai.graph.apply_human_edits — 사람 ops·사유·승인자를 rounds 에 남기고 재검사. 실패하면 다시 묻는다."""
        try:
            update = await ai_apply_human_edits(state)
        except ValueError as exc:  # 승인 API 가 check_edited_ops 로 막지만, 그사이 명세가 바뀐 경우의 안전장치
            log.warning("review %s: 사람이 고친 ops 적용 실패 — %s", state["review_id"], exc)
            await repo.update_review(state["review_id"], error=f"edited_ops 적용 실패: {exc}"[:2000])
            return Command(goto="await_human", update={"human_decision": None})
        return Command(goto="static_check", update=update)

    # --- AI 수정 커밋 -----------------------------------------------------------------------------

    async def commit_fix(state: dict[str, Any]) -> Command:
        """원본 deploy.yaml 에 applied_ops 를 적용해 PR 브랜치에 커밋하고, 그 커밋을 autofix_commit 새 검토로 넘긴다.

        커밋 객체를 먼저 만들어 SHA 를 알아낸 뒤 → 새 검토를 DB 에 넣고 → 브랜치를 옮긴다. 브랜치가 움직이면
        GitHub 이 pull_request synchronize 웹훅을 보내는데, 그때 Review API 가 같은 SHA 검토를 이미 찾을 수 있어야
        (autofix_commit 이 빠진) 중복 검토를 만들지 않는다.
        baseline 없이 생성된 명세(generated_spec)는 사람이 아직 확인하지 않았으면 새 검토에도 넘긴다 — 빠지면 봇 커밋
        재검토가 pass 로 자동 병합된다. 사람이 승인·수정한 뒤면 넘기지 않는다 (같은 확인을 두 번 묻지 않는다).
        """
        rid, ref = state["review_id"], state["spec_ref"]
        repository, path, parent = ref["repository"], ref["path"], ref["commit"]
        ops = applied_ops(state)
        try:
            pull = await _open_pull(ref)
            if is_fork_pull(pull, repository):
                raise GitHubError(f"PR #{pull['number']} 은 포크 브랜치라 자동 커밋할 수 없다")
            raw = await _retry("deploy.yaml 읽기", lambda: github.get_file(repository, path, parent))
            fixed = apply_ops(yaml.safe_load(raw), ops)  # 원본(가리지 않은 값)에 적용. ops 는 env·시크릿을 건드리지 못한다
            DeploySpec.model_validate(fixed)
            content = (f"# AI 검토 {rid} 가 고친 명세입니다. 무엇을 왜 고쳤는지는 검토 결과(rounds)에 있습니다.\n"
                       + yaml.safe_dump(fixed, sort_keys=False, allow_unicode=True))
            new_sha = await github.prepare_file_commit(repository, parent=parent, path=path, content=content,
                                                       message=f"fix(deploy): AI 검토 자동 수정 ({rid})")
        except (GitHubError, ValueError) as exc:
            return await _fail(rid, f"commit_fix: {exc}")

        new_rid = new_review_id()
        new_ref = {**ref, "commit": new_sha}
        message = build_review_requested(fixed, review_id=new_rid, spec_ref=new_ref, requested_by=f"autofix:{rid}",
                                         requested_at=datetime.now(UTC), autofix_commit=True,
                                         generated_spec=spec_unverified(state),
                                         unverified_paths=(state.get("unverified_paths") or ()) if spec_unverified(state) else ())
        await repo.insert_review(review_id=new_rid, app=message.app, target_env=message.target_env,
                                 repo_id=message.repo_id, spec_ref=new_ref, pr_head_sha=new_sha,
                                 requested_by=message.requested_by, pr_number=pull["number"])
        try:
            await github.update_branch(repository, pull["head"]["ref"], new_sha)
        except GitHubError as exc:  # 그사이 사람이 푸시했다 — 이 수정은 버린다
            await repo.update_review(new_rid, status="failed", error=f"브랜치 갱신 실패: {exc}"[:2000])
            return await _fail(rid, f"commit_fix: {exc}")
        await repo.update_review(rid, status="superseded", superseded_by=new_rid)
        metrics.finished(*app_env(state), "superseded")
        try:
            await deps.publisher.send(REQUESTED_TOPIC, message.repo_id, message.encode())
        except Exception as exc:  # noqa: BLE001 — 브랜치는 이미 옮겼다. 새 검토는 received 로 남고 review sweep 이 다시 발행한다
            log.warning("review %s: 재검토 %s 발행 실패 — review sweep 이 다시 발행한다: %s", rid, new_rid, exc)
        await _verify_status(new_rid, new_ref, "pending", "AI 검토 중 (수정 커밋)")  # 옛 SHA 상태는 그대로 둔다
        await _record_case(state)
        log.info("review %s: 수정 %d건을 %s 로 커밋 → 재검토 %s", rid, len(ops), new_sha, new_rid)
        return Command(goto=END)

    # --- CI 확인 · 병합 ---------------------------------------------------------------------------

    async def _ci_now(state: dict[str, Any]) -> str | None:
        """GitHub 에 물어본 지금의 CI 결론. 조회 실패면 예외."""
        ref = state["spec_ref"]
        suites = await _retry("check-suites 조회", lambda: github.check_suites(ref["repository"], ref["commit"]))
        return ci_conclusion(suites, deps.ci_app_slug)

    async def _after_ci(state: dict[str, Any], conclusion: str) -> Command:
        if conclusion != "success":
            return await _fail(state["review_id"], f"CI {conclusion} ({state['spec_ref']['commit']})")
        return Command(goto="confirm_ci")

    async def await_ci(state: dict[str, Any]) -> dict[str, Any]:
        await repo.update_review(state["review_id"], status="waiting_ci", final_spec=app_spec(state["deploy_spec"]))
        approved = (state.get("human_decision") or {}).get("decision") == "approved"
        await _verify_status(state["review_id"], state.get("spec_ref"), "success",
                             "사람이 승인" if approved else "AI 검토 통과")
        return {}

    async def check_ci(state: dict[str, Any]) -> Command:
        """waiting_ci 로 바꾼 뒤 이미 끝난 CI 가 있는지 본다. 끝났으면 기다리지 않는다."""
        try:
            conclusion = await _ci_now(state)
        except GitHubError as exc:
            log.warning("review %s: CI 상태 조회 실패 — 웹훅을 기다린다: %s", state["review_id"], exc)
            conclusion = None
        if conclusion is None:
            return Command(goto="wait_ci")
        log.info("review %s: CI 가 이미 끝나 있다 (%s) — 기다리지 않는다", state["review_id"], conclusion)
        return await _after_ci(state, conclusion)

    async def wait_ci(state: dict[str, Any]) -> Command:
        resumed = CiResult.model_validate(interrupt({"kind": "ci_completed", "review_id": state["review_id"]}))
        try:
            conclusion = await _ci_now(state)  # 다른 suite 가 아직 돌면 다시 기다린다
        except GitHubError as exc:  # 웹훅 한 건의 결론(suite 하나)으로 병합하지 않는다 — waiting_ci 로 돌아간다
            log.warning("review %s: CI 상태 조회 실패 — 웹훅 결론(%s)은 쓰지 않고 다시 기다린다: %s",
                        state["review_id"], resumed.conclusion, exc)
            return Command(goto="await_ci")
        if conclusion is None:
            return Command(goto="await_ci")
        return await _after_ci(state, conclusion)

    async def confirm_ci(state: dict[str, Any]) -> Command:
        """병합 직전 — check-suites 를 다시 읽어 전체가 success 인지 본다. 조회 실패·미완료면 병합하지 않고 waiting_ci."""
        rid = state["review_id"]
        try:
            conclusion = await _ci_now(state)
        except GitHubError as exc:
            log.warning("review %s: 병합 전 CI 확인 실패 — 병합하지 않고 다시 기다린다: %s", rid, exc)
            return Command(goto="await_ci")
        if conclusion is None:
            log.info("review %s: 병합 전 CI 확인 — 아직 끝나지 않은 suite 가 있다", rid)
            return Command(goto="await_ci")
        if conclusion != "success":
            return await _after_ci(state, conclusion)
        return Command(goto="guard_overlay")

    async def guard_overlay(state: dict[str, Any]) -> Command:
        """병합 전 — 지금 배포 중인 보호 리소스(ingress 등)를 지우는 명세면 병합하지 않고 blocked (10/09 sample-app#11 장애)."""
        rid = state["review_id"]
        for attempt in range(1, COMMIT_OVERLAY_MAX_ATTEMPTS + 1):
            try:
                reason = await deps.overlay_guard(state)
                break
            except TransientError as exc:
                if attempt == COMMIT_OVERLAY_MAX_ATTEMPTS:
                    raise
                log.warning("review %s: overlay 검사 일시 오류 %d — %s", rid, attempt, exc)
                await asyncio.sleep(deps.retry_backoff_seconds * 2**attempt)
        if reason is None:
            return Command(goto="merge_pr")
        log.warning("review %s: 병합 안 함 — %s", rid, reason)
        result = DeployResult(status="blocked", commit_sha=None, reason=reason)
        await repo.update_review(rid, status="blocked", error=reason, deploy_result=result)
        metrics.finished(*app_env(state), "blocked")
        await _verify_status(rid, state.get("spec_ref"), "failure", reason)
        return Command(goto=END, update={"deploy_result": result})

    async def merge_pr(state: dict[str, Any]) -> Command:
        rid, ref = state["review_id"], state["spec_ref"]
        await repo.update_review(rid, status="merging")
        try:
            pull = await _open_pull(ref)  # 병합 직전에 PR 을 다시 읽는다
            if is_fork_pull(pull, ref["repository"]):
                raise GitHubError(f"fork PR — PR #{pull['number']} 은 포크 브랜치라 병합하지 않는다")
            if pull.get("draft"):  # GitHub 이 405 로 거절한다 — 부르지 않고 verify 성공도 쓰지 않는다
                raise GitHubError(f"draft PR — PR #{pull['number']} 은 draft 라 병합하지 않는다")
            # 필수 체크라 success 가 없으면 GitHub 이 병합을 거절한다 — 병합 직전에 다시 쓴다
            if not await _verify_status(rid, ref, "success", "AI 검토 통과 — 병합", attempts=VERIFY_MAX_ATTEMPTS):
                raise GitHubError(f"커밋 상태 {VERIFY_CONTEXT} 를 쓰지 못해 병합하지 않는다")
            merge_sha = await github.merge_pull(ref["repository"], pull["number"], head_sha=ref["commit"])
        except GitHubError as exc:
            return await _fail(rid, f"merge_pr: {exc}")
        await repo.update_review(rid, merge_sha=merge_sha, merged_at=datetime.now(UTC))
        return Command(goto="commit_overlay")

    async def commit_overlay(state: dict[str, Any]) -> dict[str, Any]:
        """review_worker.commit_overlay 노드 — 원본 명세 + applied_ops → overlay → gitops 커밋. 병합 SHA 는 merge_pr 이 DB 에 남겼다."""
        for attempt in range(1, COMMIT_OVERLAY_MAX_ATTEMPTS + 1):
            try:
                result = (await deps.commit_overlay(state))["deploy_result"]
                break
            except TransientError as exc:
                if attempt == COMMIT_OVERLAY_MAX_ATTEMPTS:
                    raise
                log.warning("review %s: commit_overlay 일시 오류 %d — %s", state["review_id"], attempt, exc)
                await asyncio.sleep(deps.retry_backoff_seconds * 2**attempt)
        committed = result["status"] == "committed"
        await repo.update_review(state["review_id"], status=result["status"], deploy_result=result,
                                 gitops_commit_sha=result["commit_sha"] if committed else None,
                                 gitops_committed_at=datetime.now(UTC) if committed else None)
        metrics.finished(*app_env(state), result["status"])
        if result["status"] == "blocked":  # 병합은 됐지만 배포는 막혔다
            await _verify_status(state["review_id"], state.get("spec_ref"), "failure", result["reason"] or "blocked")
        if result["status"] == "committed" and state.get("human_decision"):  # 사람 없이 그대로 통과한 검토는 남길 판단이 없다
            await _record_case(state)
        return {"deploy_result": result}

    graph = StateGraph(ReviewState)

    def node(name: str, fn: Callable[[dict[str, Any]], Awaitable[Any]], **kwargs: Any) -> None:
        """노드마다 처리 시간·예외 지표 (review_worker_node_*)."""
        graph.add_node(name, timed_node(name, fn), **kwargs)

    node("static_check", make_static_check())
    node("retrieve_evidence", make_retrieve_evidence(deps.retriever))
    node("judge", judge)
    node("inspect_failure_cases", inspect_failure_cases)
    node("record_result", record_result)
    node("apply_patch", apply_patch)
    node("await_human", await_human)
    node("wait_human", wait_human, destinations=("apply_human_edits", "await_human", "await_ci", "commit_fix", END))
    node("apply_human_edits", apply_human_edits, destinations=("static_check", "await_human"))
    node("commit_fix", commit_fix, destinations=(END,))
    node("await_ci", await_ci)
    node("check_ci", check_ci, destinations=("wait_ci", "confirm_ci", END))
    node("wait_ci", wait_ci, destinations=("await_ci", "confirm_ci", END))
    node("confirm_ci", confirm_ci, destinations=("await_ci", "guard_overlay", END))
    node("guard_overlay", guard_overlay, destinations=("merge_pr", END))
    node("merge_pr", merge_pr, destinations=("commit_overlay", END))
    node("commit_overlay", commit_overlay)

    graph.add_edge(START, "static_check")
    graph.add_edge("static_check", "retrieve_evidence")
    graph.add_edge("retrieve_evidence", "judge")
    graph.add_edge("judge", "inspect_failure_cases")
    graph.add_edge("inspect_failure_cases", "record_result")
    graph.add_conditional_edges("record_result", route_after_result,
                                ["apply_patch", "await_human", "await_ci", "commit_fix"])
    graph.add_edge("apply_patch", "static_check")
    graph.add_edge("await_human", "wait_human")
    graph.add_edge("await_ci", "check_ci")
    graph.add_edge("commit_overlay", END)
    return graph.compile(checkpointer=checkpointer)
