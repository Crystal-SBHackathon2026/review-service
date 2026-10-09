"""package-lock.json 재생성 — 실제 npm 으로 (없으면 건너뜀). 설치·스크립트 실행 없이 잠금 파일만."""

from __future__ import annotations

import json
import shutil

import pytest

from review_api import lockfile
from review_api.lockfile import LockfileUnavailable, regenerate_lockfile

needs_npm = pytest.mark.skipif(shutil.which("npm") is None, reason="npm 없음")
EMPTY_LOCK = json.dumps({"name": "t", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
                         "packages": {"": {"name": "t", "version": "1.0.0"}}})


@needs_npm
async def test_lockfile_gets_new_dependency_without_running_scripts() -> None:
    # postinstall 이 돌면 실패하도록 — --ignore-scripts 라 돌지 않는다
    package = {"name": "t", "version": "1.0.0", "dependencies": {"ms": "^2.1.3"},
               "scripts": {"postinstall": "exit 1", "preinstall": "exit 1"}}
    result = json.loads(await regenerate_lockfile(json.dumps(package), EMPTY_LOCK))
    assert result["packages"]["node_modules/ms"]["version"].startswith("2.")
    assert result["packages"][""]["dependencies"] == {"ms": "^2.1.3"}


@needs_npm
async def test_unknown_package_is_unavailable_not_a_crash() -> None:
    package = {"name": "t", "version": "1.0.0", "dependencies": {"no-such-pkg-review-service-zzz": "^1.0.0"}}
    with pytest.raises(LockfileUnavailable, match="npm 실패"):
        await regenerate_lockfile(json.dumps(package), EMPTY_LOCK)


async def test_missing_npm_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lockfile.shutil, "which", lambda _name: None)
    with pytest.raises(LockfileUnavailable, match="npm 이 없다"):
        await regenerate_lockfile("{}", EMPTY_LOCK)
