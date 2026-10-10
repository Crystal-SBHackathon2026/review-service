"""환경별 검토를 취합하는 유일한 수정 커밋·머지·릴리스 주체. 재시작은 DB 상태에서 이어 간다."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging

import yaml
from pydantic import ValidationError

from review_ai.patching import apply_ops
from review_ai.spec.deploy_spec import AppSpec
from review_ai.verdict import applied_ops
from review_common.deployment_requests import RequestBusy
from review_common.github import GitHubError, ProtectedFileRemoval
from review_common.request_dispatch import DEPLOYMENT_BRANCH, dispatch_request, load_request, supersede_request
from review_worker.graph import ci_conclusion, VERIFY_CONTEXT

log = logging.getLogger(__name__)


class RequestCoordinator:
    def __init__(self, repo, github, publisher, gitops, registry, *, ci_app_slug="github-actions"):
        self.repo, self.gh, self.publisher, self.gitops = repo, github, publisher, gitops
        self.registry, self.ci_app_slug = registry, ci_app_slug

    async def status(self, row, state, description):
        await self.gh.create_commit_status(row["repository"], row["head_sha"], state=state,
                                           context=VERIFY_CONTEXT, description=description)

    async def advance(self, rid):
        row = await self.repo.get_request(rid)
        if row is None:
            return
        async with self.repo.request_lock(row["repository"], row["pr_number"]):
            row = await self.repo.get_request(rid)
            if row["state"] in {"committed", "blocked", "superseded"}:
                return
            try:
                await self._advance(row)
            except (ValidationError, yaml.YAMLError):
                await self.repo.update_request(rid, state="blocked", error="invalid deployment specification")
                await self.status(row, "failure", "Deployment request validation failed")
            except (ValueError, ProtectedFileRemoval) as exc:
                await self.repo.update_request(rid, state="blocked", error=str(exc)[:1000])
                await self.status(row, "failure", "Deployment request blocked; inspect request status")
            except Exception:
                # Keep the last durable state. A poll retries safely after transient failures.
                log.exception("request %s: coordinator retry", rid)
                await self.repo.update_request(rid, error="coordinator operation failed; will retry")

    async def _advance(self, row):
        rid, repository, number, head = row["request_id"], row["repository"], row["pr_number"], row["head_sha"]
        pull = await self.gh.get_pull(repository, number)
        if (pull.get("head", {}).get("repo") or {}).get("full_name") != repository:
            raise ValueError("fork PR is not supported")
        if (pull.get("base") or {}).get("ref") != DEPLOYMENT_BRANCH:
            raise ValueError("deployment PR must target main")
        if row["state"] == "fixing":
            return await self.finish_fix(row, pull)
        merged = bool(pull.get("merged_at") or pull.get("merged"))
        if not row["merge_sha"]:
            if pull["head"]["sha"] != head and pull.get("state") == "open":
                for previous in await self.repo.requests_for_pr(repository, number):
                    if (previous["state"] == "fixing" and previous["pending_sha"] == head
                            and previous["head_sha"] == pull["head"]["sha"]):
                        # The successor is durable before the ref moves. A restart or another
                        # worker may visit it first; its predecessor owns installing this SHA.
                        await self.status(row, "pending", "Waiting for the prepared fix commit to reach the PR")
                        return
            if pull["head"]["sha"] != head or (pull.get("state") != "open" and not merged):
                await supersede_request(self.repo, rid, "PR head changed or PR closed")
                return
            if pull.get("draft"):
                await self.status(row, "pending", "Draft PR: deployment request is held")
                return
            if merged:
                if row["state"] != "merging":
                    raise ValueError("PR was merged outside the coordinator")
                await self.repo.update_request(rid, state="waiting_image", merge_sha=pull["merge_commit_sha"])
                row = await self.repo.get_request(rid)
            else:
                children = await self.repo.request_children(rid)
                if len(children) != len(row["targets"]):
                    await dispatch_request(self.repo, self.gh, self.publisher, repository, number, head,
                                           row["manifest_path"], row["requested_by"], self.registry,
                                           fix_count=row["fix_count"])
                    return
                expected = {t["env"]: t["path"] for t in row["targets"]}
                if {c["target_env"]: c["spec_ref"]["path"] for c in children} != expected:
                    raise ValueError("environment review coverage does not match selected targets")
                if any(c["status"] in {"failed", "blocked", "rejected", "superseded"} for c in children):
                    raise ValueError("an environment review did not pass")
                if any(c["status"] != "waiting_ci" for c in children):
                    await self.status(row, "pending", "Waiting for all selected environment reviews")
                    return
                if any(c["pr_head_sha"] != head for c in children):
                    raise ValueError("environment reviews do not share the final PR head")
                _, specs = await load_request(self.gh, repository, head, row["manifest_path"], self.registry)
                fixes = {}
                for child in children:
                    ops = applied_ops(child)
                    if not ops:
                        continue
                    path = child["spec_ref"]["path"]
                    raw = await self.gh.get_file(repository, path, head)
                    fixed = AppSpec.model_validate(apply_ops(yaml.safe_load(raw), ops))
                    target = next(t for t in row["targets"] if t["env"] == child["target_env"])
                    if (fixed.target.env, fixed.target.region) != (target["env"], target["region"]):
                        raise ValueError("a patch changed a selected target")
                    fixes[path] = yaml.safe_dump(fixed.model_dump(mode="json", exclude_none=True), sort_keys=False)
                if fixes:
                    if row["fix_count"] >= 2:
                        raise ValueError("automatic fix commit limit reached")
                    sha = await self.gh.prepare_files_commit(repository, parent=head, files=fixes,
                                                            message=f"fix(deploy): all environment reviews ({rid})")
                    await self.repo.update_request(rid, state="fixing", pending_sha=sha)
                    return await self.finish_fix(await self.repo.get_request(rid), pull)
                conclusion = ci_conclusion(await self.gh.check_suites(repository, head), self.ci_app_slug)
                if conclusion is None:
                    await self.status(row, "pending", "Waiting for final PR head CI")
                    return
                if conclusion != "success":
                    raise ValueError("final PR head CI failed")
                snapshot = await self.gitops.preflight(specs, row["targets"], head)
                # Preflight may take time. Re-read PR and CI immediately before merge.
                if ci_conclusion(await self.gh.check_suites(repository, head), self.ci_app_slug) != "success":
                    await self.status(row, "pending", "Waiting for final CI confirmation")
                    return
                latest = await self.gh.get_pull(repository, number)
                if (latest["head"]["sha"] != head or latest.get("draft") or latest.get("state") != "open"
                        or (latest.get("base") or {}).get("ref") != DEPLOYMENT_BRANCH):
                    await supersede_request(self.repo, rid, "PR changed during preflight")
                    return
                await self.repo.update_request(rid, state="merging", error=None, expected_snapshot=snapshot)
                await self.status(row, "success", "All selected environment reviews and preflight passed")
                merge_sha = await self.gh.merge_pull(repository, number, head_sha=head)
                await self.repo.update_request(rid, state="waiting_image", merge_sha=merge_sha)
                row = await self.repo.get_request(rid)
        # Main CI builds and pushes a version tagged with the *merge* SHA. Never release a PR-head image.
        children = await self.repo.request_children(rid)
        for child in children:
            await self.repo.update_review(child["review_id"], merge_sha=row["merge_sha"], merged_at=datetime.now(UTC))
        conclusion = ci_conclusion(await self.gh.check_suites(repository, row["merge_sha"]), self.ci_app_slug)
        if conclusion is None:
            return
        if conclusion != "success":
            # Can be retried after rerunning image CI, without merging the PR again.
            await self.repo.update_request(rid, error="merge commit image CI failed; rerun image CI")
            return
        _, specs = await load_request(self.gh, repository, head, row["manifest_path"], self.registry)
        if not row["gitops_commit_sha"]:
            sha = await self.gitops.commit(specs, row["targets"], row["merge_sha"], rid, expected_snapshot=row["expected_snapshot"])
            await self.repo.update_request(rid, gitops_commit_sha=sha)
            row = await self.repo.get_request(rid)
        for child in children:
            await self.repo.update_review(child["review_id"], status="committed", gitops_commit_sha=row["gitops_commit_sha"],
                                         gitops_committed_at=datetime.now(UTC),
                                         deploy_result=dict(status="committed", commit_sha=row["gitops_commit_sha"], reason=None))
        await self.repo.update_request(rid, state="committed", error=None)

    async def finish_fix(self, row, pull):
        head, pending = row["head_sha"], row["pending_sha"]
        if pull["head"]["sha"] not in {head, pending} or pull.get("state") != "open":
            await supersede_request(self.repo, row["request_id"], "PR changed while committing fixes")
            return
        if pull.get("draft"):
            await self.status(row, "pending", "Draft PR: deployment request is held")
            return
        new = await dispatch_request(self.repo, self.gh, self.publisher, row["repository"], row["pr_number"], pending,
                                     row["manifest_path"], f"autofix:{row['request_id']}", self.registry,
                                     fix_count=row["fix_count"] + 1)
        if pull["head"]["sha"] == head:
            try:
                await self.gh.update_branch(row["repository"], pull["head"]["ref"], pending)
            except GitHubError:
                latest = await self.gh.get_pull(row["repository"], row["pr_number"])
                if latest["head"]["sha"] != pending:
                    if (latest["head"]["sha"] != head or latest.get("state") != "open"
                            or (latest.get("base") or {}).get("ref") != DEPLOYMENT_BRANCH):
                        await supersede_request(self.repo, new, "PR changed before the fix commit was installed")
                        await supersede_request(self.repo, row["request_id"], "PR changed while committing fixes")
                        return
                    # A transport error or 5xx need not mean a different PR head. Keep the
                    # prepared successor and pending SHA so the next poll can retry the ref.
                    raise
        await supersede_request(self.repo, row["request_id"], "all environments must review the new SHA")
        await self.status(await self.repo.get_request(new), "pending", "Re-reviewing all selected environments")

    async def retry(self, retry):
        parent = await self.repo.get_request(retry["request_id"])
        async with self.repo.request_lock(parent["repository"], parent["pr_number"]):
            try:
                if parent["state"] != "committed":
                    raise ValueError("only a committed request can retry deployment")
                children = await self.repo.request_children(parent["request_id"])
                child = next(c for c in children if c["target_env"] == retry["target_env"])
                last = await self.repo.last_deploy_event(review_id=child["review_id"], target_env=child["target_env"])
                if last and last["kind"] == "healthy":
                    await self.repo.finish_request_retry(retry["retry_id"], sha=child["gitops_commit_sha"])
                    return  # Already recovered before the queued retry was consumed.
                _, specs = await load_request(self.gh, parent["repository"], parent["head_sha"],
                                             parent["manifest_path"], self.registry)
                env = retry["target_env"]
                sha = await self.gitops.commit({env: specs[env]}, [t for t in parent["targets"] if t["env"] == env],
                                               parent["merge_sha"], parent["request_id"], retry_id=retry["retry_id"])
                await self.repo.update_review(child["review_id"], gitops_commit_sha=sha)
                await self.repo.finish_request_retry(retry["retry_id"], sha=sha)
            except (ValidationError, yaml.YAMLError):
                await self.repo.finish_request_retry(retry["retry_id"], error="invalid deployment specification")
            except (ValueError, ProtectedFileRemoval) as exc:
                await self.repo.finish_request_retry(retry["retry_id"], error=str(exc)[:1000])

    async def sweep_once(self):
        for row in await self.repo.pending_requests():
            try:
                await self.advance(row["request_id"])
            except RequestBusy:
                pass
        for retry in await self.repo.pending_request_retries():
            try:
                await self.retry(retry)
            except RequestBusy:
                pass
            except Exception:
                log.exception("deployment retry %s: will retry", retry["retry_id"])

    async def sweep(self):
        while True:
            try:
                await self.sweep_once()
            except Exception:
                log.exception("deployment request sweep failed; will retry")
            await asyncio.sleep(5)
