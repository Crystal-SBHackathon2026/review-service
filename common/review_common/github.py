"""GitHub REST — Review API 는 deploy.yaml 을 읽고, 워커는 CI 상태 확인·AI 수정 커밋·PR 병합을 한다.

GITHUB_TOKEN 이 없으면 미인증(시간당 60회). 워커는 앱 레포 Contents·Pull requests 쓰기 권한이 있는 토큰을 쓴다.
"""

from __future__ import annotations

import base64
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

    async def get_file_blob(self, repository: str, path: str, ref: str) -> tuple[str, str]:
        """(파일 원문, blob SHA). blob SHA 는 같은 파일을 커밋(put_file)할 때 필요하다."""
        resp = await self._request("GET", f"/repos/{repository}/contents/{path.lstrip('/')}", params={"ref": ref})
        if resp.status_code == 404:
            raise SpecNotFound(f"{repository}@{ref} 에 {path} 가 없다", 404)
        if resp.status_code != 200:
            raise GitHubError(f"GitHub contents API {resp.status_code}", resp.status_code)
        body = resp.json()
        return base64.b64decode(body["content"]).decode("utf-8"), body["sha"]

    async def put_file(self, repository: str, path: str, *, branch: str, content: str, blob_sha: str,
                       message: str) -> str:
        """PUT /repos/{owner}/{repo}/contents/{path} — 브랜치에 파일 한 개를 커밋한다. 새 커밋 SHA 반환.

        blob_sha 가 브랜치의 현재 파일과 다르면(그사이 누가 고쳤으면) GitHub 이 409 로 거절한다.
        """
        resp = await self._request("PUT", f"/repos/{repository}/contents/{path.lstrip('/')}", json={
            "message": message, "branch": branch, "sha": blob_sha,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        })
        if resp.status_code not in (200, 201):
            raise GitHubError(f"{path} 커밋 실패 {resp.status_code}", resp.status_code)
        return resp.json()["commit"]["sha"]

    async def check_suites(self, repository: str, sha: str) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/commits/{sha}/check-suites — 그 커밋의 CI 진행 상태."""
        resp = await self._request("GET", f"/repos/{repository}/commits/{sha}/check-suites",
                                   params={"per_page": 100})
        if resp.status_code != 200:
            raise GitHubError(f"check-suites 조회 실패 {resp.status_code}", resp.status_code)
        return resp.json()["check_suites"]

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
