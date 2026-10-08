"""GitHub REST — Review API 는 deploy.yaml 을 읽고, 워커는 PR 을 찾아 병합한다. GITHUB_TOKEN 이 없으면 미인증(시간당 60회)."""

from __future__ import annotations

import os
from typing import Any

import httpx

API = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SpecNotFound(GitHubError):
    pass


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

    async def get_file(self, repository: str, path: str, ref: str) -> str:
        """그 커밋의 파일 원문. GET /repos/{owner}/{repo}/contents/{path}?ref=<sha>"""
        resp = await self._request("GET", f"/repos/{repository}/contents/{path.lstrip('/')}",
                                   params={"ref": ref}, headers={"Accept": "application/vnd.github.raw+json"})
        if resp.status_code == 404:
            raise SpecNotFound(f"{repository}@{ref} 에 {path} 가 없다", 404)
        if resp.status_code != 200:
            raise GitHubError(f"GitHub contents API {resp.status_code}", resp.status_code)
        return resp.text

    async def pulls_for_commit(self, repository: str, sha: str) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/commits/{sha}/pulls"""
        resp = await self._request("GET", f"/repos/{repository}/commits/{sha}/pulls")
        if resp.status_code != 200:
            raise GitHubError(f"커밋의 PR 조회 실패 {resp.status_code}", resp.status_code)
        return resp.json()

    async def merge_pull(self, repository: str, number: int, *, head_sha: str) -> str:
        """PUT /repos/{owner}/{repo}/pulls/{n}/merge — sha 를 같이 보내 그사이 head 가 바뀌면 GitHub 이 거절한다. 병합 SHA 반환."""
        resp = await self._request("PUT", f"/repos/{repository}/pulls/{number}/merge",
                                   json={"merge_method": "merge", "sha": head_sha})
        if resp.status_code != 200:
            message = resp.json().get("message", "") if resp.headers.get("content-type", "").startswith(
                "application/json") else ""
            raise GitHubError(f"PR #{number} 병합 실패 {resp.status_code} {message}".strip(), resp.status_code)
        return resp.json()["sha"]
