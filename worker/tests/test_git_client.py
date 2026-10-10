"""GitHubGitClient — Git Data API 로 gitops 커밋. httpx MockTransport 로 GitHub 를 흉내 낸다."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from review_ai.errors import TransientError
from review_common.github import (GitHubClient, GitHubError, GitHubGitClient, ProtectedFileRemoval, git_blob_sha,
                                  is_protected)

GITOPS = "Crystal-SBHackathon2026/gitops"
DIR = "apps/sample-app/overlays/aws"


class FakeGitHubApi:
    """gitops main 브랜치 하나. ref 갱신 충돌·5xx 를 주입할 수 있다."""

    def __init__(self, files: dict[str, str]) -> None:
        self.head = "c0"
        self.trees = {"t0": dict(files)}
        self.commits = {"c0": "t0"}
        self.conflicts = 0
        self.server_errors = 0
        self.created_trees: list[list[dict[str, Any]]] = []
        self.created_commits = 0
        self.ref_updates = 0
        self.conflict_files: dict[str, str] | None = None  # 충돌 때 다른 곳이 먼저 커밋한 main 의 파일 (기본: 처음 그대로)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.server_errors:
            self.server_errors -= 1
            return httpx.Response(502)
        path, method = request.url.path, request.method
        prefix = f"/repos/{GITOPS}/git/"
        assert path.startswith(prefix), path
        rest = path[len(prefix):]
        if method == "GET" and rest == "ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": self.head}})
        if method == "GET" and rest.startswith("commits/"):
            return httpx.Response(200, json={"tree": {"sha": self.commits[rest.split("/")[1]]}})
        if method == "GET" and rest.startswith("trees/"):
            files = self.trees[rest.split("/")[1]]
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"path": p, "type": "blob", "sha": git_blob_sha(c)} for p, c in files.items()]})
        body = json.loads(request.content or b"{}")
        if method == "POST" and rest == "trees":
            self.created_trees.append(body["tree"])
            files = dict(self.trees[body["base_tree"]])
            for e in body["tree"]:
                if e.get("sha", "") is None:
                    files.pop(e["path"])
                else:
                    files[e["path"]] = e["content"]
            sha = f"t{len(self.trees)}"
            self.trees[sha] = files
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and rest == "commits":
            sha = f"c{len(self.commits)}"
            self.commits[sha] = body["tree"]
            self.created_commits += 1
            return httpx.Response(201, json={"sha": sha})
        if method == "PATCH" and rest == "refs/heads/main":
            assert body["force"] is False
            self.ref_updates += 1
            if self.conflicts:
                self.conflicts -= 1
                self.head = f"other{self.conflicts}"  # 다른 CI 가 먼저 커밋했다
                tree = "t0"
                if self.conflict_files is not None:
                    tree = f"t{len(self.trees)}"
                    self.trees[tree] = dict(self.conflict_files)
                self.commits[self.head] = tree
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            self.head = body["sha"]
            return httpx.Response(200, json={})
        raise AssertionError(f"{method} {path}")


def client(api: FakeGitHubApi, cls: type[GitHubGitClient] = GitHubGitClient) -> GitHubGitClient:
    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(api.handler))
    return cls(GitHubClient("t", client=http), gitops_repo=GITOPS, backoff_seconds=0)


def files_at_head(api: FakeGitHubApi) -> dict[str, str]:
    return api.trees[api.commits[api.head]]


async def test_commit_files_adds_updates_and_deletes_only_inside_directory() -> None:
    api = FakeGitHubApi({f"{DIR}/kustomization.yaml": "old", f"{DIR}/pvc-data.yaml": "pvc",
                         f"{DIR}/ingress.yaml": "same", "apps/other/keep.yaml": "keep"})
    sha = await client(api).commit_files(DIR, {"kustomization.yaml": "new", "ingress.yaml": "same"}, "msg")

    assert sha == api.head
    assert files_at_head(api) == {f"{DIR}/kustomization.yaml": "new", f"{DIR}/ingress.yaml": "same",
                                  "apps/other/keep.yaml": "keep"}  # 명세에서 빠진 pvc 는 지우고, 밖은 그대로
    [entries] = api.created_trees
    assert {e["path"] for e in entries} == {f"{DIR}/kustomization.yaml", f"{DIR}/pvc-data.yaml"}  # 같은 파일은 안 올린다


async def test_no_change_returns_current_head_without_commit() -> None:
    api = FakeGitHubApi({f"{DIR}/kustomization.yaml": "same"})
    assert await client(api).commit_files(DIR, {"kustomization.yaml": "same"}, "msg") == "c0"
    assert api.created_trees == [] and api.ref_updates == 0


async def test_ref_conflict_rebuilds_from_new_main() -> None:
    api = FakeGitHubApi({f"{DIR}/kustomization.yaml": "old"})
    api.conflicts = 1
    sha = await client(api).commit_files(DIR, {"kustomization.yaml": "new"}, "msg")

    assert api.ref_updates == 2 and sha == api.head
    assert files_at_head(api)[f"{DIR}/kustomization.yaml"] == "new"


async def test_conflict_five_times_is_transient() -> None:
    api = FakeGitHubApi({})
    api.conflicts = 5
    with pytest.raises(TransientError, match="5회"):
        await client(api).commit_files(DIR, {"kustomization.yaml": "new"}, "msg")


async def test_server_error_is_transient() -> None:
    api = FakeGitHubApi({})
    api.server_errors = 1
    with pytest.raises(TransientError):
        await client(api).commit_files(DIR, {"kustomization.yaml": "new"}, "msg")


async def test_protected_file_added_during_conflict_is_not_removed_on_retry() -> None:
    """검사 뒤 다른 곳이 ingress.yaml 을 넣고 ref 가 충돌 → 재시도는 새 main 기준으로 보고 지우지 않고 멈춘다."""
    api = FakeGitHubApi({f"{DIR}/kustomization.yaml": "old"})
    api.conflicts = 1
    api.conflict_files = {f"{DIR}/kustomization.yaml": "old", f"{DIR}/ingress.yaml": "ingress"}

    with pytest.raises(ProtectedFileRemoval) as exc:
        await client(api).commit_files(DIR, {"kustomization.yaml": "new"}, "msg", protected=("ingress.yaml",))

    assert exc.value.paths == ["ingress.yaml"]
    assert not isinstance(exc.value, (TransientError, GitHubError))
    # 첫 시도의 tree·commit·ref 갱신(422) 하나씩 — 재시도에서는 아무것도 만들지 않았다
    assert (len(api.created_trees), api.created_commits, api.ref_updates) == (1, 1, 1)
    assert files_at_head(api)[f"{DIR}/ingress.yaml"] == "ingress"


async def test_protected_file_removal_on_first_attempt_makes_no_commit() -> None:
    api = FakeGitHubApi({f"{DIR}/kustomization.yaml": "old", f"{DIR}/ingress.yaml": "ingress"})

    with pytest.raises(ProtectedFileRemoval):
        await client(api).commit_files(DIR, {"kustomization.yaml": "new"}, "msg", protected=["ingress.yaml"])

    assert (api.created_trees, api.created_commits, api.ref_updates) == ([], 0, 0)


async def test_protected_file_kept_by_rendered_files_commits_normally() -> None:
    api = FakeGitHubApi({f"{DIR}/kustomization.yaml": "old", f"{DIR}/ingress.yaml": "old"})
    api.conflicts = 1
    api.conflict_files = {f"{DIR}/kustomization.yaml": "old", f"{DIR}/ingress.yaml": "other"}

    sha = await client(api).commit_files(DIR, {"kustomization.yaml": "new", "ingress.yaml": "new"}, "msg",
                                         protected=("ingress.yaml",))

    assert sha == api.head and api.ref_updates == 2
    assert files_at_head(api) == {f"{DIR}/kustomization.yaml": "new", f"{DIR}/ingress.yaml": "new"}


async def test_protected_pattern_blocks_pvc_removal() -> None:
    """보호 목록은 패턴이다 — pvc-*.yaml 은 어떤 볼륨 이름의 PVC 든 지우지 않는다."""
    api = FakeGitHubApi({f"{DIR}/kustomization.yaml": "old", f"{DIR}/pvc-data.yaml": "pvc",
                         f"{DIR}/pvc-cache.yaml": "pvc"})

    with pytest.raises(ProtectedFileRemoval) as exc:
        await client(api).commit_files(DIR, {"kustomization.yaml": "new", "pvc-cache.yaml": "pvc"}, "msg",
                                       protected=("ingress.yaml", "pvc-*.yaml"))

    assert exc.value.paths == ["pvc-data.yaml"]
    assert (api.created_trees, api.created_commits, api.ref_updates) == ([], 0, 0)


async def test_unprotected_file_removal_still_commits() -> None:
    api = FakeGitHubApi({f"{DIR}/kustomization.yaml": "old", f"{DIR}/hpa.yaml": "hpa"})

    await client(api).commit_files(DIR, {"kustomization.yaml": "new"}, "msg", protected=("ingress.yaml", "pvc-*.yaml"))

    assert files_at_head(api) == {f"{DIR}/kustomization.yaml": "new"}


@pytest.mark.parametrize(("name", "expected"), [
    ("ingress.yaml", True), ("pvc-data.yaml", True), ("pvc-a-b.yaml", True),
    ("pvc.yaml", False), ("pvc-data.yml", False), ("PVC-data.yaml", False), ("sub/pvc-data.yaml", False),
    ("kustomization.yaml", False),
])
def test_is_protected(name: str, expected: bool) -> None:
    assert is_protected(name, ("ingress.yaml", "pvc-*.yaml")) is expected


async def test_read_file_missing_is_file_not_found() -> None:
    http = httpx.AsyncClient(base_url="https://api.github.com",
                             transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(FileNotFoundError):
        await GitHubGitClient(GitHubClient("t", client=http)).read_file("o/r", "deploy.yaml", "abc1234")


def test_blob_sha_matches_git() -> None:
    assert git_blob_sha("hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"  # git hash-object


async def test_list_files_only_direct_children_of_directory() -> None:
    api = FakeGitHubApi({f"{DIR}/kustomization.yaml": "k", f"{DIR}/ingress.yaml": "i", f"{DIR}/sub/x.yaml": "x",
                         "apps/sample-app/base/rollout.yaml": "r"})
    assert await client(api).list_files(DIR) == ["ingress.yaml", "kustomization.yaml"]
    assert await client(api).list_files("apps/new-app/overlays/aws") == []


async def test_list_files_server_error_is_transient() -> None:
    api = FakeGitHubApi({})
    api.server_errors = 1
    with pytest.raises(TransientError):
        await client(api).list_files(DIR)
