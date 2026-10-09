"""deploy.yaml 이 없거나 비었거나 형식이 깨진 PR — spec_intakes 에 남기고, 생성 커밋을 올리거나 PR 에 실패를 표시한다.

    웹훅은 GitHub 조직 단위라 gitops·인프라 레포 PR 도 온다 → deploy.yaml 이 '없는' PR 은 배포 대상 레포
    (baseline 이 있거나 INTAKE_REPOSITORIES 에 있는 레포)만 intake 로 연다. 나머지는 예전처럼 skip.
    파일은 있는데 비었거나 깨졌으면 배포하려는 레포로 보고 연다.
    pull_request 웹훅 → open_intake: processing 행만 만들고 202. 처리는 process_intake (BackgroundTasks).
        GitHub 웹훅은 10초 안에 응답해야 한다 — 복구(LLM)가 붙으면 넘을 수 있어 웹훅 안에서 하지 않는다.
    process_intake
        - 루프 방지: head 가 intake 가 만든 커밋이면 다시 만들지 않는다 (LOOP_GUARD)
        - 포크 PR 은 브랜치에 쓸 수 없다 (FORK_PR)
        - 앱·대상: 그 레포의 가장 최근 baseline, 없으면 DEFAULT_TARGET(예 aws/ap-northeast-2). 둘 다 없으면 NO_TARGET
        - baseline 이 없는 새 앱은 PR head 의 Dockerfile·의존성·CI·소스를 읽어 확인된 값만 채운다
          (review_ai.intake.analyze). 하나라도 애매하면 UNVERIFIED 로 커밋하지 않는다
        - missing·empty: review_ai.intake.prepare_intake 가 생성. yaml_error·schema_error: PR head 의 원문을 읽어
          review_ai.intake.repair.repair_intake 가 LLM 으로 형식만 고친다(값은 코드 게이트가 원문과 대조).
          LLM 이 없으면(repair_llm None) REPAIR_UNAVAILABLE
        - 생성·복구하면 PR 브랜치에 커밋 → synchronize 웹훅이 그 SHA 를 일반 검토로
          시작한다 (autofix_commit 아님). 커밋 SHA 는 브랜치를 옮기기 전에 행에 남긴다 — 웹훅이 먼저 와도 연결된다
    PR 표시: 커밋 상태 review-service/intake. 토큰에 권한이 없으면 로그만 남기고 기록은 그대로 둔다.
    파드가 처리 중에 죽으면 processing 행이 남는다 → resume_stale_intakes 가 STALE_AFTER 뒤에 다시 처리한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

import yaml
from pydantic import ValidationError

from review_ai.intake import KIND_LABELS, commit_message, prepare_intake
from review_ai.errors import TransientError
from review_ai.intake.analyze import Finding, analyze_repository, files_to_read
from review_ai.intake.repair import repair_intake
from review_ai.preparation import GenerationContext
from review_ai.spec.deploy_spec import Baseline, DeploySpec, Target
from review_common.github import GitHubError, RefConflict
from review_common.ids import new_intake_id
from review_common.repository import baseline_for

if TYPE_CHECKING:
    from review_api.app import ApiDeps

log = logging.getLogger(__name__)

STATUS_CONTEXT = "review-service/intake"
STALE_AFTER = timedelta(minutes=2)  # LLM 복구(최대 수십 초)보다 넉넉하게
SWEEP_EVERY_SECONDS = 60.0
REPAIRABLE = ("yaml_error", "schema_error")
READ_CONCURRENCY = 8  # 레포 분석 파일 읽기 — GitHub 은 동시 요청이 많으면 secondary rate limit 을 건다


class IntakeGitHub(Protocol):
    async def list_files(self, repository: str, ref: str) -> list[str]: ...

    async def get_file(self, repository: str, path: str, ref: str) -> str: ...

    async def prepare_file_commit(self, repository: str, *, parent: str, path: str, content: str,
                                  message: str) -> str: ...

    async def update_branch(self, repository: str, branch: str, sha: str) -> None: ...

    async def create_commit_status(self, repository: str, sha: str, *, state: str, context: str, description: str,
                                   target_url: str | None = None) -> None: ...


class SpecProblem(Exception):
    """명세를 검토로 넘길 수 없다 — kind 는 spec_intakes.kind."""

    def __init__(self, kind: str, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.errors = errors or []


def validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return json.loads(exc.json(include_url=False, include_input=False))


def _yaml_error(exc: yaml.YAMLError) -> dict[str, Any]:
    """줄·칸·문제만. str(exc) 에는 원문 줄이 그대로 붙어 비밀이 섞일 수 있다."""
    mark = getattr(exc, "problem_mark", None)
    return {"type": "yaml", "line": mark.line + 1 if mark else None, "column": mark.column + 1 if mark else None,
            "problem": getattr(exc, "problem", None) or type(exc).__name__}


def load_spec(raw: str, path: str) -> dict[str, Any]:
    """원문 → 명세 dict. 비었거나 깨졌으면 SpecProblem."""
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SpecProblem("yaml_error", f"{path} YAML 파싱 실패", [_yaml_error(exc)]) from exc
    if loaded is None or loaded == {}:  # 빈 파일·주석뿐·null·{} — preparation 의 '빈 입력'과 같다
        raise SpecProblem("empty", f"{path} 가 비었다")
    try:
        DeploySpec.model_validate(loaded)
    except ValidationError as exc:
        raise SpecProblem("schema_error", "deploy_spec 형식 오류", validation_errors(exc)) from exc
    return loaded


async def expects_spec(deps: ApiDeps, repository: str) -> bool:
    """deploy.yaml 이 있어야 하는 레포인가 — 배포된 적이 있거나(baseline) 명시한 레포."""
    if repository in deps.intake_repositories:
        return True
    return await deps.repo.latest_baseline_for_repository(repository) is not None


async def open_intake(deps: ApiDeps, payload: dict[str, Any], problem: SpecProblem, path: str) -> dict[str, Any]:
    pr, repo = payload["pull_request"], payload["repository"]
    repository, head_sha = repo["full_name"], pr["head"]["sha"]
    intake_id = new_intake_id()
    created = await deps.repo.insert_intake(
        intake_id=intake_id, repository=repository,
        head_repository=(pr["head"].get("repo") or {}).get("full_name") or "",  # 삭제된 포크는 null
        pr_number=pr["number"], head_sha=head_sha, head_ref=pr["head"]["ref"], path=path, kind=problem.kind,
        errors=problem.errors, requested_by=payload["sender"]["login"])
    if not created:
        existing = await deps.repo.find_intake_by_head(repository, head_sha)
        return {"skipped": "already taken", "intake_id": existing and existing["intake_id"]}
    # 이전 커밋의 끝나지 않은 검토는 더 이상 PR 의 내용이 아니다 — 승인·CI 로 이어지지 않게 넘긴다
    superseded = await deps.repo.supersede_open(repository=repository, pr_number=pr["number"], superseded_by=None)
    return {"intake_id": intake_id, "kind": problem.kind, "superseded": superseded}


def _done(status: str, reason: str, message: str, details: Any = (), **extra: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, "message": message, "details": list(details), **extra}


Context = tuple[GenerationContext, Baseline | None, tuple[Finding, ...]]


async def _context(deps: ApiDeps, github: IntakeGitHub, repository: str, head_sha: str) -> Context | None:
    row = await deps.repo.latest_baseline_for_repository(repository)
    if row is not None:
        baseline = Baseline.model_validate(baseline_for(row))
        return GenerationContext(repository=repository, target=baseline.spec.target), baseline, ()
    if not deps.default_target:
        return None
    env, _, region = deps.default_target.partition("/")
    base = GenerationContext(repository=repository, target=Target(env=env, region=region))
    tree = await github.list_files(repository, head_sha)
    paths = files_to_read(tree)
    limit = asyncio.Semaphore(READ_CONCURRENCY)

    async def read(path: str) -> str:
        async with limit:
            return await github.get_file(repository, path, head_sha)

    texts = await asyncio.gather(*map(read, paths))
    analysis = analyze_repository(base, tree, dict(zip(paths, texts, strict=True)))
    return analysis.context, None, analysis.findings


async def _decide(deps: ApiDeps, row: dict[str, Any]) -> dict[str, Any]:
    repository, head_sha = row["repository"], row["head_sha"]
    if await deps.repo.find_intake_by_result_commit(repository, head_sha) is not None:
        return _done("rejected", "LOOP_GUARD", "review-service 가 만든 커밋인데 다시 명세가 필요하다 — 자동 생성을 멈춘다")
    if row["head_repository"] != repository:
        return _done("rejected", "FORK_PR", "포크 PR 브랜치에는 커밋할 수 없다 — deploy.yaml 을 직접 추가해라")
    if deps.github is None:
        return _done("rejected", "COMMIT_UNAVAILABLE", "GitHub 쓰기 클라이언트가 없어 생성 커밋을 올릴 수 없다")
    if row["kind"] in REPAIRABLE and deps.repair_llm is None:
        return _done("rejected", "REPAIR_UNAVAILABLE",
                     f"{KIND_LABELS[row['kind']]} — 자동 복구(LLM)가 설정되지 않았다. 오류를 고쳐 다시 올려라")
    found = await _context(deps, deps.github, repository, head_sha)
    if found is None:
        return _done("rejected", "NO_TARGET", "배포 대상 환경을 모른다 — 이전 배포(baseline)도 DEFAULT_TARGET 도 없다")
    context, baseline, findings = found
    if row["kind"] in REPAIRABLE:
        raw = await deps.github.get_file(repository, row["path"], head_sha)
        outcome = await repair_intake(row["kind"], raw, context=context, baseline=baseline, llm=deps.repair_llm)
    else:
        outcome = prepare_intake(row["kind"], context=context, baseline=baseline, findings=findings)
    if outcome.action == "rejected":
        return _done("rejected", outcome.reason, outcome.message, outcome.details)
    # 다시 처리하는 행이면 이미 만든 커밋을 쓴다 — 같은 커밋으로 ref 를 옮기는 건 몇 번 해도 같다
    commit = row["result_commit_sha"] or await deps.github.prepare_file_commit(
        repository, parent=head_sha, path=row["path"], content=outcome.content, message=commit_message(outcome))
    await deps.repo.link_intake(row["intake_id"], result_commit_sha=commit)
    try:
        await deps.github.update_branch(repository, row["head_ref"], commit)
    except RefConflict:
        # GitHub 은 브랜치 보호로 막혀도 422 를 준다 — 둘 다 이 커밋에서는 더 할 게 없다
        return _done("rejected", "BRANCH_MOVED",
                     "PR 브랜치를 옮기지 못했다(그사이 새 커밋 또는 브랜치 보호) — 새 커밋이 오면 다시 판단한다")
    return _done(outcome.action, outcome.reason, outcome.message, outcome.details, result_commit_sha=commit)


async def process_intake(deps: ApiDeps, intake_id: str) -> None:
    row = await deps.repo.get_intake(intake_id)
    if row is None or row["status"] != "processing":
        return
    await _post_status(deps, row, "pending", f"{KIND_LABELS[row['kind']]} — 처리 중")
    try:
        fields = await _decide(deps, row)
    except Exception as exc:  # 행을 processing 으로 두지 않는다 — 원인은 로그에
        log.exception("intake %s: 처리 실패", intake_id)
        transient = isinstance(exc, TransientError) or isinstance(exc, GitHubError) and exc.transient
        fields = _done("failed", "ERROR", f"{KIND_LABELS[row['kind']]} — 처리 중 오류({type(exc).__name__})"
                       + (". 새 커밋을 올리면 다시 처리한다" if transient else ""))
    if await deps.repo.finish_intake(intake_id, **fields):
        state = {"generated": "success", "repaired": "success", "failed": "error"}.get(fields["status"], "failure")
        await _post_status(deps, row, state, fields["message"])


async def _post_status(deps: ApiDeps, row: dict[str, Any], state: str, description: str) -> None:
    if deps.github is None:
        return
    target_url = f"{deps.public_url.rstrip('/')}/intakes/{row['intake_id']}" if deps.public_url else None
    try:
        await deps.github.create_commit_status(row["repository"], row["head_sha"], state=state,
                                               context=STATUS_CONTEXT, description=description,
                                               target_url=target_url)
    except GitHubError as exc:  # Commit statuses 권한이 없는 토큰이면 여기서 끝난다 — 기록은 남아 있다
        log.warning("intake %s: 커밋 상태 기록 실패 %s", row["intake_id"], exc)


async def resume_stale_intakes(deps: ApiDeps) -> list[str]:
    """처리 중 파드가 죽어 STALE_AFTER 넘게 processing 으로 남은 행을 다시 처리한다. 다시 처리한 intake_id 들."""
    rows = await deps.repo.claim_stale_intakes(STALE_AFTER)
    for row in rows:
        await process_intake(deps, row["intake_id"])
    return [row["intake_id"] for row in rows]


async def sweep_stale_intakes(deps: ApiDeps) -> None:
    """API 가 떠 있는 동안 resume_stale_intakes 를 주기적으로 돌린다."""
    while True:
        try:
            await resume_stale_intakes(deps)
        except Exception:
            log.exception("intake sweep 실패")
        await asyncio.sleep(SWEEP_EVERY_SECONDS)
