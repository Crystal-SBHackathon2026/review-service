"""GitHub REST — Review API 는 deploy.yaml·레포 파일을 읽고 명세 생성 커밋·PR 커밋 상태를 쓰고, 워커는 CI 상태 확인·AI 수정 커밋·PR 병합·gitops 커밋을 한다.

GITHUB_TOKEN 이 없으면 미인증(시간당 60회). 워커는 앱 레포·gitops 레포 Contents·Pull requests 쓰기 권한이 있는
토큰(oneaction/gitops-token)을 쓴다.

커밋은 Git Data API 로 만든다 (blob·tree·commit 을 만들고 ref 를 옮긴다). contents API 와 달리 여러 파일 추가·삭제를
한 커밋에 담을 수 있고, ref 를 옮기기 전에 새 커밋 SHA 를 알 수 있다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Mapping
from typing import Any

import httpx

from review_ai.errors import TransientError

API = "https://api.github.com"
DEFAULT_GITOPS_REPO = "Crystal-SBHackathon2026/gitops"
FILE_MODE = "100644"
log = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def transient(self) -> bool:
        """네트워크 오류·5xx·429 — 다시 하면 될 수 있다."""
        return self.status_code is None or self.status_code >= 500 or self.status_code == 429


class SpecNotFound(GitHubError):
    pass


class RefConflict(GitHubError):
    """ref 갱신이 fast-forward 가 아니다 — 그사이 다른 커밋이 브랜치에 들어갔다."""


def git_blob_sha(content: str) -> str:
    """git 이 계산하는 blob SHA. 내용이 같은 파일은 다시 올리지 않으려고 쓴다."""
    data = content.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class GitHubClient:
    def __init__(self, token: str | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "crystal-review-service"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(base_url=API, headers=headers, timeout=15.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub 요청 실패: {type(exc).__name__}") from exc

    async def _json(self, method: str, url: str, what: str, ok: tuple[int, ...] = (200,), **kwargs: Any) -> Any:
        resp = await self._request(method, url, **kwargs)
        if resp.status_code not in ok:
            raise GitHubError(f"{what} 실패 {resp.status_code}", resp.status_code)
        return resp.json()

    # --- 앱 레포: 읽기·CI·PR ---------------------------------------------------------------------

    async def get_file(self, repository: str, path: str, ref: str) -> str:
        """그 커밋의 파일 원문. GET /repos/{owner}/{repo}/contents/{path}?ref=<sha>"""
        resp = await self._request("GET", f"/repos/{repository}/contents/{path.lstrip('/')}",
                                   params={"ref": ref}, headers={"Accept": "application/vnd.github.raw+json"})
        if resp.status_code == 404:
            raise SpecNotFound(f"{repository}@{ref} 에 {path} 가 없다", 404)
        if resp.status_code != 200:
            raise GitHubError(f"GitHub contents API {resp.status_code}", resp.status_code)
        return resp.text

    async def list_files(self, repository: str, ref: str) -> list[str]:
        """그 커밋의 모든 파일 경로 — 레포 분석이 읽을 파일을 고른다."""
        return list(await self.tree_blobs(repository, await self.commit_tree_sha(repository, ref)))

    async def check_suites(self, repository: str, sha: str) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/commits/{sha}/check-suites — 그 커밋의 CI 진행 상태."""
        body = await self._json("GET", f"/repos/{repository}/commits/{sha}/check-suites", "check-suites 조회",
                                params={"per_page": 100})
        return body["check_suites"]

    async def pulls_for_commit(self, repository: str, sha: str) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/commits/{sha}/pulls"""
        return await self._json("GET", f"/repos/{repository}/commits/{sha}/pulls", "커밋의 PR 조회")

    async def merge_pull(self, repository: str, number: int, *, head_sha: str) -> str:
        """PUT /repos/{owner}/{repo}/pulls/{n}/merge — sha 를 같이 보내 그사이 head 가 바뀌면 GitHub 이 거절한다. 병합 SHA 반환."""
        resp = await self._request("PUT", f"/repos/{repository}/pulls/{number}/merge",
                                   json={"merge_method": "merge", "sha": head_sha})
        if resp.status_code != 200:
            message = resp.json().get("message", "") if resp.headers.get("content-type", "").startswith(
                "application/json") else ""
            raise GitHubError(f"PR #{number} 병합 실패 {resp.status_code} {message}".strip(), resp.status_code)
        return resp.json()["sha"]

    async def create_commit_status(self, repository: str, sha: str, *, state: str, context: str, description: str,
                                   target_url: str | None = None) -> None:
        """POST /repos/{owner}/{repo}/statuses/{sha} — PR 의 검사 목록에 한 줄. 토큰에 Commit statuses 쓰기 권한 필요.

        state: pending·success·failure·error. description 은 GitHub 이 140자에서 자른다.
        """
        body: dict[str, Any] = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        await self._json("POST", f"/repos/{repository}/statuses/{sha}", "커밋 상태 기록", ok=(201,), json=body)

    # --- Git Data API ------------------------------------------------------------------------------

    async def branch_sha(self, repository: str, branch: str) -> str:
        """GET /repos/{owner}/{repo}/git/ref/heads/{branch}"""
        body = await self._json("GET", f"/repos/{repository}/git/ref/heads/{branch}", f"{branch} ref 조회")
        return body["object"]["sha"]

    async def commit_tree_sha(self, repository: str, commit_sha: str) -> str:
        body = await self._json("GET", f"/repos/{repository}/git/commits/{commit_sha}", "커밋 조회")
        return body["tree"]["sha"]

    async def tree_blobs(self, repository: str, tree_sha: str) -> dict[str, str]:
        """트리 전체의 파일 경로 → blob SHA."""
        body = await self._json("GET", f"/repos/{repository}/git/trees/{tree_sha}", "트리 조회",
                                params={"recursive": "1"})
        if body.get("truncated"):
            raise GitHubError("트리가 너무 커서 잘렸다")
        return {e["path"]: e["sha"] for e in body["tree"] if e["type"] == "blob"}

    async def create_tree(self, repository: str, base_tree: str, entries: list[dict[str, Any]]) -> str:
        """POST /repos/{owner}/{repo}/git/trees — entries 의 sha=None 은 파일 삭제."""
        body = await self._json("POST", f"/repos/{repository}/git/trees", "트리 생성", ok=(201,),
                                json={"base_tree": base_tree, "tree": entries})
        return body["sha"]

    async def create_commit(self, repository: str, *, message: str, tree: str, parents: list[str]) -> str:
        body = await self._json("POST", f"/repos/{repository}/git/commits", "커밋 생성", ok=(201,),
                                json={"message": message, "tree": tree, "parents": parents})
        return body["sha"]

    async def update_branch(self, repository: str, branch: str, sha: str) -> None:
        """PATCH /repos/{owner}/{repo}/git/refs/heads/{branch} (force=false). fast-forward 가 아니면 RefConflict."""
        resp = await self._request("PATCH", f"/repos/{repository}/git/refs/heads/{branch}",
                                   json={"sha": sha, "force": False})
        if resp.status_code == 422:
            raise RefConflict(f"{branch} 가 그사이 움직였다 (non-fast-forward)", 422)
        if resp.status_code != 200:
            raise GitHubError(f"{branch} ref 갱신 실패 {resp.status_code}", resp.status_code)

    async def prepare_file_commit(self, repository: str, *, parent: str, path: str, content: str,
                                  message: str) -> str:
        """parent 위에 파일 하나를 바꾼 커밋을 만든다. 브랜치는 옮기지 않는다 — 새 커밋 SHA 만 돌려준다."""
        return await self.prepare_files_commit(repository, parent=parent, files={path: content}, message=message)

    async def prepare_files_commit(self, repository: str, *, parent: str, files: Mapping[str, str | None],
                                   message: str) -> str:
        """parent 위에 여러 파일을 바꾼(None 이면 지운) 커밋 하나를 만든다. 브랜치는 옮기지 않는다."""
        if not files:
            raise ValueError("커밋할 파일이 없다")
        entries = [{"path": path.lstrip("/"), "mode": FILE_MODE, "type": "blob",
                    **({"sha": None} if content is None else {"content": content})}
                   for path, content in sorted(files.items())]
        base_tree = await self.commit_tree_sha(repository, parent)
        tree = await self.create_tree(repository, base_tree, entries)
        return await self.create_commit(repository, message=message, tree=tree, parents=[parent])


class GitHubGitClient:
    """review_worker.commit_overlay.GitClient 구현 — 앱 레포 읽기, gitops 레포 main 에 overlay 커밋.

    gitops 에 커밋하는 곳이 셋(sample-app CI, review-service CI, commit_overlay)이라 ref 갱신이 충돌할 수 있다.
    충돌하면 main 을 다시 읽어 처음부터 다시 만든다 (최대 max_attempts 회).
    네트워크 오류·5xx·재시도해도 풀리지 않는 충돌은 TransientError 로 올린다.
    """

    def __init__(self, github: GitHubClient, *, gitops_repo: str | None = None, branch: str = "main",
                 max_attempts: int = 5, backoff_seconds: float = 0.5) -> None:
        self._gh = github
        self.gitops_repo = gitops_repo or os.environ.get("GITOPS_REPO") or DEFAULT_GITOPS_REPO
        self.branch = branch
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds

    async def read_file(self, repository: str, path: str, ref: str) -> str:
        try:
            return await self._gh.get_file(repository, path, ref)
        except SpecNotFound as exc:
            raise FileNotFoundError(str(exc)) from exc
        except GitHubError as exc:
            if exc.transient:
                raise TransientError(str(exc)) from exc
            raise

    async def commit_files(self, directory: str, files: Mapping[str, str], message: str) -> str:
        directory = directory.strip("/")
        try:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    return await self._commit_once(directory, files, message)
                except RefConflict:
                    log.info("gitops %s 충돌 %d/%d — main 을 다시 읽는다", self.branch, attempt, self.max_attempts)
                    if attempt < self.max_attempts:
                        await asyncio.sleep(self.backoff_seconds * attempt)
        except GitHubError as exc:
            if exc.transient:
                raise TransientError(str(exc)) from exc
            raise
        raise TransientError(f"gitops {self.branch} ref 충돌이 {self.max_attempts}회 계속됐다")

    async def _commit_once(self, directory: str, files: Mapping[str, str], message: str) -> str:
        repo = self.gitops_repo
        head = await self._gh.branch_sha(repo, self.branch)
        base_tree = await self._gh.commit_tree_sha(repo, head)
        existing = {p: sha for p, sha in (await self._gh.tree_blobs(repo, base_tree)).items()
                    if p.startswith(directory + "/")}
        wanted = {f"{directory}/{name}": content for name, content in files.items()}
        entries: list[dict[str, Any]] = [
            {"path": p, "mode": FILE_MODE, "type": "blob", "content": content}
            for p, content in sorted(wanted.items()) if existing.get(p) != git_blob_sha(content)
        ]
        entries += [{"path": p, "mode": FILE_MODE, "type": "blob", "sha": None}
                    for p in sorted(existing) if p not in wanted]  # 생성물이라 명세에서 빠진 파일은 지운다
        if not entries:
            return head  # 바뀐 게 없으면 커밋하지 않는다
        tree = await self._gh.create_tree(repo, base_tree, entries)
        commit = await self._gh.create_commit(repo, message=message, tree=tree, parents=[head])
        await self._gh.update_branch(repo, self.branch, commit)
        return commit
