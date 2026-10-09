"""코드 패치가 package.json 의존성을 바꾸면 package-lock.json 을 npm 으로 다시 만든다.

LLM 은 잠금 파일을 쓸 수 없다(무결성 해시·하위 의존성). 잠금 파일이 낡으면 앱 CI 와 Docker 빌드의 `npm ci` 가 실패한다.
- 빈 임시 디렉터리에 package.json·package-lock.json 두 파일만 둔다 — 레포의 .npmrc·스크립트·소스는 없다
- `--package-lock-only --ignore-scripts` — 설치도, 패키지 스크립트 실행도 하지 않는다. 레지스트리 메타데이터만 읽는다
- HOME·캐시를 임시 디렉터리로 돌려 다른 요청의 설정·캐시와 섞이지 않게 한다
의존성 이름·버전은 코드 게이트(review_ai.transform.gate)가 이미 계획한 범위(^x.y.z)로 막았다.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

NPM_TIMEOUT_SECONDS = 120.0
NPM_ARGS = ("install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error")


class LockfileUnavailable(RuntimeError):
    """npm 이 없거나 실패했다 — 잠금 파일을 만들 수 없으니 패치를 커밋하지 않는다."""


async def regenerate_lockfile(package_json: str, lock: str) -> str:
    npm = shutil.which("npm")
    if npm is None:
        raise LockfileUnavailable("npm 이 없다")
    with tempfile.TemporaryDirectory(prefix="lock-") as tmp:
        work = Path(tmp) / "app"
        work.mkdir()
        (work / "package.json").write_text(package_json, encoding="utf-8")
        (work / "package-lock.json").write_text(lock, encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "npm_config_cache": str(Path(tmp) / "cache"),
               "npm_config_userconfig": str(Path(tmp) / "npmrc"), "npm_config_update_notifier": "false"}
        proc = await asyncio.create_subprocess_exec(npm, *NPM_ARGS, cwd=work, env=env,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), NPM_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise LockfileUnavailable(f"npm 이 {NPM_TIMEOUT_SECONDS:.0f}초 안에 끝나지 않았다") from exc
        if proc.returncode != 0:
            tail = stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["?"]
            raise LockfileUnavailable(f"npm 실패({proc.returncode}): {tail[0][:200]}")
        result = (work / "package-lock.json").read_text(encoding="utf-8")
    try:
        json.loads(result)
    except ValueError as exc:
        raise LockfileUnavailable("npm 이 만든 잠금 파일을 읽을 수 없다") from exc
    return result
