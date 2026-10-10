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
          (review_ai.intake.analyze). 확인하지 못한 값은 후보값으로 커밋하고 그 경로(unverified_paths)를 행에 남긴다
        - missing·empty: review_ai.intake.prepare_intake 가 생성. yaml_error·schema_error: PR head 의 원문을 읽어
          review_ai.intake.repair.repair_intake 가 LLM 으로 형식만 고친다(값은 코드 게이트가 원문과 대조).
          LLM 이 없으면(repair_llm None) REPAIR_UNAVAILABLE
        - 새 앱이 대상 환경에서 그대로 못 돌면(SQLite 인데 볼륨 없음 등) review_ai.transform 이 코드 패치를 만든다
          (LLM + 코드 게이트). 통과하면 package-lock.json 을 npm 으로 다시 만들어 코드·명세를 한 커밋에 올린다
        - 생성·복구하면 PR 브랜치에 커밋 → synchronize 웹훅이 그 SHA 를 일반 검토로
          시작한다 (autofix_commit 아님). 커밋 SHA 는 브랜치를 옮기기 전에 행에 남긴다 — 웹훅이 먼저 와도 연결된다
        - baseline 으로 만들었는지(baseline_used)·확인하지 못한 경로(unverified_paths)도 같이 남긴다. 확인하지 못한 값이
          있는 생성 명세(missing·empty)가 든 PR 의 검토는 pass 여도 needs_human(GENERATED_SPEC_UNVERIFIED) —
          app.on_pull_request. 전부 확인된 생성 명세는 일반 검토처럼 자동 병합·배포된다
    PR 표시: 커밋 상태 review-service/intake. 토큰에 권한이 없으면 로그만 남기고 기록은 그대로 둔다.
    파드가 처리 중에 죽으면 processing 행이 남는다 → resume_stale_intakes 가 STALE_AFTER 뒤에 다시 처리한다.
        처리 중에는 HEARTBEAT_EVERY 마다 updated_at 을 찍는다 — 레포 분석·LLM 이 STALE_AFTER 를 넘겨도 살아 있는
        처리를 sweep 이 가져가지 않게. 그래도 두 곳이 겹치면(heartbeat 가 DB 오류로 빠짐 등) 커밋 직전에 행을 다시 읽어
        끝난 행이면 멈추고, 커밋은 행에 먼저 이어진 것 하나만 쓴다 (link_intake 는 result_commit_sha 를 덮지 않는다).
        처리할 때마다 파드를 죽이는 행(npm 잠금 파일 재생성·대용량 분석 중 OOM 등)을 끝없이 다시 가져가지 않게
        attempts(웹훅 1, sweep 이 가져갈 때마다 +1)가 MAX_INTAKE_ATTEMPTS 를 넘으면 처리하지 않고
        failed(RETRY_EXHAUSTED) + 커밋 상태 error 로 끝낸다. 새 커밋은 새 행이라 다시 1번부터 처리한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

import yaml
from pydantic import ValidationError

from review_ai.intake import KIND_LABELS, IntakeOutcome, commit_message, prepare_intake
from review_ai.errors import TransientError
from review_ai.intake.analyze import Finding, analyze_repository, files_to_read
from review_ai.catalog import load_targets
from review_ai.intake.repair import MAX_RAW_CHARS, repair_intake
from review_ai.preparation import GenerationContext, app_name
from review_ai.transform import TransformOutcome, apply_to_context, plan_transform, transform_repository
from review_api import metrics
from review_api.lockfile import LockfileUnavailable
from review_ai.spec.deploy_spec import Baseline, DeploySpec, Target
from review_common.github import FileTooLarge, GitHubError, RefConflict
from review_common.ids import new_intake_id
from review_common.repository import baseline_for

if TYPE_CHECKING:
    from review_api.app import ApiDeps

log = logging.getLogger(__name__)

STATUS_CONTEXT = "review-service/intake"
STALE_AFTER = timedelta(minutes=5)  # LLM 복구(수십 초)·코드 패치(파일 전체 재작성, 1~2분)·npm 잠금 파일보다 넉넉하게
SWEEP_EVERY_SECONDS = 60.0
HEARTBEAT_EVERY = STALE_AFTER / 4  # DB 가 한두 번 실패해도 STALE_AFTER 안에 다시 찍는다
MAX_INTAKE_ATTEMPTS = 3  # 처음 처리 1번 + sweep 이 다시 처리 2번. 넘으면 RETRY_EXHAUSTED
REPAIRABLE = ("yaml_error", "schema_error")
READ_CONCURRENCY = 8  # 레포 분석 파일 읽기 — GitHub 은 동시 요청이 많으면 secondary rate limit 을 건다
# 코드 패치가 풀 수 있는 미해결 항목 — 다른 항목이 남으면 어차피 사람 확인으로 가니 코드 패치(LLM)는 하지 않는다
TRANSFORM_RESOLVES = frozenset({"/database", "/requirements"})
OTHER_LOCKFILES = ("yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json")
MAX_READ_BYTES = 256 * 1024  # 레포 분석 파일 하나 — 넘으면 읽지 않은 파일로 둔다('없음' 결론을 막는다)
MAX_SPEC_BYTES = 4 * MAX_RAW_CHARS  # 복구할 deploy.yaml — 글자 수 상한은 repair 가 다시 본다 (UTF-8 한 글자 ≤ 4바이트)
MAX_LOCK_BYTES = 16 * 1024 * 1024  # 다시 만들 package-lock.json — 큰 모노레포도 수 MB. 넘으면 커밋하지 않는다


class IntakeGitHub(Protocol):
    async def list_files(self, repository: str, ref: str) -> list[str]: ...

    async def get_file(self, repository: str, path: str, ref: str, *, max_bytes: int | None = None) -> str: ...

    async def prepare_file_commit(self, repository: str, *, parent: str, path: str, content: str,
                                  message: str) -> str: ...

    async def prepare_files_commit(self, repository: str, *, parent: str, files: dict[str, str | None],
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


def unverified_of(intake: dict[str, Any] | None) -> list[str]:
    """spec_intakes 행의 확인하지 못한 경로. 0012 전 행(NULL)은 빈 목록 — 무엇을 확인할지 모른다는 뜻."""
    return list((intake or {}).get("unverified_paths") or [])


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


@dataclass(frozen=True)
class RepoFiles:
    """새 앱의 PR head — 레포 분석이 읽은 파일. 코드 패치가 같은 파일을 쓴다."""

    tree: tuple[str, ...]
    files: dict[str, str]


Context = tuple[GenerationContext, Baseline | None, tuple[Finding, ...], RepoFiles | None]


async def _context(deps: ApiDeps, github: IntakeGitHub, repository: str, head_sha: str) -> Context | None:
    row = await deps.repo.latest_baseline_for_repository(repository)
    if row is not None:
        baseline = Baseline.model_validate(baseline_for(row))
        return GenerationContext(repository=repository, target=baseline.spec.target), baseline, (), None
    if not deps.default_target:
        return None
    env, _, region = deps.default_target.partition("/")
    base = GenerationContext(repository=repository, target=Target(env=env, region=region))
    tree = await github.list_files(repository, head_sha)
    paths = files_to_read(tree)
    limit = asyncio.Semaphore(READ_CONCURRENCY)

    async def read(path: str) -> str | None:
        async with limit:
            try:
                return await github.get_file(repository, path, head_sha, max_bytes=MAX_READ_BYTES)
            except FileTooLarge:
                log.info("%s@%s: %s 가 커서 분석에서 뺀다", repository, head_sha, path)
                return None

    texts = await asyncio.gather(*map(read, paths))
    files = {p: text for p, text in zip(paths, texts, strict=True) if text is not None}
    analysis = analyze_repository(base, tree, files)
    return analysis.context, None, analysis.findings, RepoFiles(tuple(tree), files)


async def _transform(deps: ApiDeps, github: IntakeGitHub, row: dict[str, Any], context: GenerationContext,
                     findings: tuple[Finding, ...], repo: RepoFiles) -> TransformOutcome | None:
    """새 앱의 코드 패치. 필요 없거나 패치로 풀 수 없는 레포면 None — LLM 을 부르지 않는다."""
    caps = load_targets()[context.target.env]
    app = context.name or app_name(context.repository)
    plan = plan_transform(app, caps, repo.tree, repo.files)
    unresolved = {f.path for f in findings if not f.resolved}
    if not plan.items or unresolved - (TRANSFORM_RESOLVES if plan.database else frozenset()):
        return None
    outcome = await transform_repository(app, caps, repo.tree, repo.files, deps.transform_llm)
    if outcome.action != "patched" or "package.json" not in outcome.files:
        return outcome
    if other := [f for f in OTHER_LOCKFILES if f in repo.tree]:
        return _lock_rejected(outcome, f"npm 이 아닌 잠금 파일({', '.join(other)})은 다시 만들 수 없다")
    if "package-lock.json" not in repo.tree:
        return outcome  # 잠금 파일 없는 레포 — CI 가 npm install 을 쓴다
    try:
        lock = await github.get_file(row["repository"], "package-lock.json", row["head_sha"], max_bytes=MAX_LOCK_BYTES)
    except FileTooLarge:
        return _lock_rejected(outcome, f"package-lock.json 이 {MAX_LOCK_BYTES // (1024 * 1024)}MB 를 넘어 다시 만들지 않는다")
    try:
        new_lock = await deps.lockfile(outcome.files["package.json"] or "", lock)
    except LockfileUnavailable as exc:
        return _lock_rejected(outcome, str(exc))
    return replace(outcome, files={**outcome.files, "package-lock.json": new_lock},
                   details=outcome.details + ({"path": "package-lock.json", "source": "npm",
                                               "reason": "바뀐 의존성으로 다시 만들었다 (--package-lock-only --ignore-scripts)"},))


def _lock_rejected(outcome: TransformOutcome, why: str) -> TransformOutcome:
    return replace(outcome, action="rejected", reason="LOCKFILE_UNAVAILABLE", files={},
                   message=f"{outcome.message} — 코드 패치는 통과했지만 package-lock.json 을 다시 만들지 못했다",
                   details=({"code": "LOCKFILE_UNAVAILABLE", "path": "package-lock.json", "message": why},))


def _commit_files(outcome: IntakeOutcome, path: str, transform: TransformOutcome | None) -> dict[str, str | None]:
    files: dict[str, str | None] = dict(transform.files) if transform and transform.action == "patched" else {}
    files[path] = outcome.content
    return files


def _message(outcome: IntakeOutcome, transform: TransformOutcome | None) -> str:
    if transform is None or transform.action != "patched":
        return commit_message(outcome)
    lines = [f"feat: {transform.message} — 코드 패치와 deploy.yaml 생성", "", "코드 패치 (review-service, LLM + 코드 게이트)"]
    lines += [f"- {d['path']}: {d['reason']}" for d in transform.details]
    lines += ["", "명세", *commit_message(outcome).splitlines()[2:]]
    return "\n".join(lines).rstrip() + "\n"


async def _decide(deps: ApiDeps, row: dict[str, Any]) -> dict[str, Any] | None:
    """처리 결과(finish_intake 필드). 그사이 다른 곳이 이 행을 끝냈으면 None."""
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
    context, baseline, findings, repo = found
    transform: TransformOutcome | None = None
    if row["kind"] in REPAIRABLE:
        try:
            raw = await deps.github.get_file(repository, row["path"], head_sha, max_bytes=MAX_SPEC_BYTES)
        except FileTooLarge:
            return _done("rejected", "REPAIR_REJECTED",
                         f"{KIND_LABELS[row['kind']]} — 파일이 커서 자동 복구하지 않았다. 직접 고쳐라",
                         [{"code": "TOO_LARGE", "path": "", "message": f"{MAX_RAW_CHARS}자까지만 자동 복구한다"}])
        outcome = await repair_intake(row["kind"], raw, context=context, baseline=baseline, llm=deps.repair_llm)
    else:
        if repo is not None:  # 다시 처리하는 행도 같은 판단을 다시 한다 (커밋은 아래에서 이미 만든 것을 쓴다)
            transform = await _transform(deps, deps.github, row, context, findings, repo)
            if transform is not None:
                context, findings = apply_to_context(context, findings, transform)
        outcome = prepare_intake(row["kind"], context=context, baseline=baseline, findings=findings)
    # 코드 패치가 막혀 그 항목(DB 등)이 확인되지 않은 채 남았다 — 패치가 풀려던 문제를 후보값으로 덮지 않는다
    if transform is not None and transform.action == "rejected" and (outcome.action == "rejected" or outcome.unverified):
        return _done("rejected", transform.reason, transform.message, transform.details)
    if outcome.action == "rejected":
        return _done("rejected", outcome.reason, outcome.message, outcome.details)
    # 분석·LLM 이 길었다 — 그사이 sweep 이 가져가 끝냈으면 두 번째 커밋을 만들지 않는다
    current = await deps.repo.get_intake(row["intake_id"])
    if current is None or current["status"] != "processing":
        return None
    # 다시 처리하는 행이면 이미 만든 커밋을 쓴다 — 같은 커밋으로 ref 를 옮기는 건 몇 번 해도 같다
    commit = current["result_commit_sha"] or await deps.github.prepare_files_commit(
        repository, parent=head_sha, files=_commit_files(outcome, row["path"], transform),
        message=_message(outcome, transform))
    # 동시에 만든 커밋이 먼저 이어졌으면 그 커밋으로 옮긴다 — 행의 SHA 와 브랜치가 어긋나면 생성 커밋 웹훅이 끊긴다
    commit = await deps.repo.link_intake(row["intake_id"], result_commit_sha=commit, baseline_used=baseline is not None,
                                         unverified_paths=list(outcome.unverified_paths)) or commit
    try:
        await deps.github.update_branch(repository, row["head_ref"], commit)
    except RefConflict:
        # GitHub 은 브랜치 보호로 막혀도 422 를 준다 — 둘 다 이 커밋에서는 더 할 게 없다
        return _done("rejected", "BRANCH_MOVED",
                     "PR 브랜치를 옮기지 못했다(그사이 새 커밋 또는 브랜치 보호) — 새 커밋이 오면 다시 판단한다")
    # 확인하지 못한 항목을 앞에 둔다 — /intakes/{id} 를 연 사람이 먼저 볼 것
    if transform is not None and transform.action == "patched":
        return _done(outcome.action, "TRANSFORMED", f"{transform.message} — 코드 패치·deploy.yaml 커밋",
                     outcome.unverified + transform.details + outcome.details, result_commit_sha=commit)
    skipped = ({"path": "(코드 패치)", "source": transform.reason, "reason": transform.message},) if transform else ()
    return _done(outcome.action, outcome.reason, outcome.message, outcome.unverified + outcome.details + skipped,
                 result_commit_sha=commit)


async def process_intake(deps: ApiDeps, intake_id: str) -> None:
    row = await deps.repo.get_intake(intake_id)
    if row is None or row["status"] != "processing":
        return
    await _post_status(deps, row, "pending", f"{KIND_LABELS[row['kind']]} — 처리 중")
    heartbeat = asyncio.create_task(_heartbeat(deps, intake_id))
    try:
        fields = await _decide(deps, row)
    except Exception as exc:  # 행을 processing 으로 두지 않는다 — 원인은 로그에
        log.exception("intake %s: 처리 실패", intake_id)
        transient = isinstance(exc, TransientError) or isinstance(exc, GitHubError) and exc.transient
        fields = _done("failed", "ERROR", f"{KIND_LABELS[row['kind']]} — 처리 중 오류({type(exc).__name__})"
                       + (". 새 커밋을 올리면 다시 처리한다" if transient else ""))
    finally:
        heartbeat.cancel()
    if fields is None:
        log.info("intake %s: 다른 곳이 먼저 끝냈다", intake_id)
        return
    await _finish(deps, row, fields)


async def _finish(deps: ApiDeps, row: dict[str, Any], fields: dict[str, Any]) -> bool:
    """processing 인 행을 끝내고 PR 에 결과 커밋 상태를 남긴다. 그사이 다른 곳이 끝냈으면 False."""
    if not await deps.repo.finish_intake(row["intake_id"], **fields):
        return False
    state = {"generated": "success", "repaired": "success", "failed": "error"}.get(fields["status"], "failure")
    await _post_status(deps, row, state, fields["message"])
    return True


async def _heartbeat(deps: ApiDeps, intake_id: str) -> None:
    """처리가 끝날 때까지 updated_at 을 찍는다 (process_intake 가 취소한다)."""
    while True:
        await asyncio.sleep(HEARTBEAT_EVERY.total_seconds())
        try:
            await deps.repo.touch_intake(intake_id)
        except Exception:  # 한 번 빠져도 다음 박자에 다시 찍는다 — 처리는 계속한다
            log.warning("intake %s: heartbeat 실패", intake_id, exc_info=True)


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
    """처리 중 파드가 죽어 STALE_AFTER 넘게 processing 으로 남은 행을 다시 처리한다. 다시 처리한 intake_id 들.

    attempts 가 MAX_INTAKE_ATTEMPTS 를 넘은 행은 처리하지 않고 failed(RETRY_EXHAUSTED)로 끝낸다 (돌려주는 목록에 없다)."""
    rows = await deps.repo.claim_stale_intakes(STALE_AFTER)
    exhausted = [row for row in rows if row["attempts"] > MAX_INTAKE_ATTEMPTS]
    retried = [row for row in rows if row["attempts"] <= MAX_INTAKE_ATTEMPTS]
    for row in exhausted:
        await _give_up(deps, row)
    if retried:
        metrics.safe(metrics.INTAKE_SWEEP_RECOVERED.inc, len(retried))
    for row in retried:
        log.info("intake %s: 다시 처리 %d/%d", row["intake_id"], row["attempts"], MAX_INTAKE_ATTEMPTS)
        await process_intake(deps, row["intake_id"])
    return [row["intake_id"] for row in retried]


async def _give_up(deps: ApiDeps, row: dict[str, Any]) -> None:
    """처리하다 매번 멈춘 행 — 다시 처리하면 또 파드를 죽일 수 있다. failed 로 끝내고 PR 에 error 를 남긴다."""
    log.error("intake %s: %d번 처리해도 끝나지 않아 다시 처리하지 않는다", row["intake_id"], MAX_INTAKE_ATTEMPTS)
    fields = _done("failed", "RETRY_EXHAUSTED",
                   f"{KIND_LABELS[row['kind']]} — 처리 {MAX_INTAKE_ATTEMPTS}번이 모두 중간에 멈췄다(파드 재시작 등). "
                   "deploy.yaml 을 직접 추가하거나 새 커밋을 올려라")
    if await _finish(deps, row, fields):
        metrics.safe(metrics.INTAKE_SWEEP_FAILED.inc)


async def sweep_stale_intakes(deps: ApiDeps) -> None:
    """API 가 떠 있는 동안 resume_stale_intakes 를 주기적으로 돌린다."""
    while True:
        try:
            await resume_stale_intakes(deps)
        except Exception:
            log.exception("intake sweep 실패")
        await asyncio.sleep(SWEEP_EVERY_SECONDS)
