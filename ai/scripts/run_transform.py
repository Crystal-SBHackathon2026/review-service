"""로컬 레포 체크아웃으로 코드 패치를 한 번 돌린다 — 계획·게이트 결과를 보고, 통과하면 결과 파일을 쓴다.

    .venv/bin/python scripts/run_transform.py ../../sample-todo --env aws              # 계획만 (LLM 없음)
    .venv/bin/python scripts/run_transform.py ../../sample-todo --env aws --claude --out /tmp/todo-patched

--claude 는 ANTHROPIC_API_KEY 가 필요하다. --out 은 원래 레포를 복사한 뒤 패치를 적용한 디렉터리를 만든다
(package-lock.json 은 다시 만들지 않는다 — npm install --package-lock-only --ignore-scripts 로 직접).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review_ai.catalog import load_targets  # noqa: E402
from review_ai.intake.analyze import files_to_read  # noqa: E402
from review_ai.judge.llm import ClaudeLLM  # noqa: E402
from review_ai.transform import TRANSFORM_MAX_TOKENS, plan_transform, transform_repository  # noqa: E402
from review_ai.transform.prompt import TransformOutput  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("--env", choices=["aws", "gcp", "local"], default="aws")
    parser.add_argument("--app")
    parser.add_argument("--claude", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    tree = subprocess.run(["git", "-C", str(args.repo), "ls-files"], check=True, capture_output=True,
                          text=True).stdout.split()
    files = {p: (args.repo / p).read_text(encoding="utf-8") for p in files_to_read(tree)}
    app = args.app or args.repo.resolve().name
    caps = load_targets()[args.env]
    plan = plan_transform(app, caps, tree, files)
    print(json.dumps({"items": [i.code for i in plan.items], "skipped": list(plan.skipped),
                      "database": plan.database.model_dump(mode="json") if plan.database else None,
                      "tables": list(plan.tables)}, ensure_ascii=False, indent=1))
    if not args.claude:
        return 0
    llm = ClaudeLLM(output=TransformOutput, max_tokens=TRANSFORM_MAX_TOKENS,
                    **({"model": args.model} if args.model else {}))
    usage: dict[str, int] = {}

    class Counting:
        model = llm.model

        async def complete(self, request):  # type: ignore[no-untyped-def]
            response = await llm.complete(request)
            usage.update(response.usage)
            return response

    outcome = asyncio.run(transform_repository(app, caps, tree, files, Counting()))
    print(json.dumps({"action": outcome.action, "reason": outcome.reason, "message": outcome.message,
                      "details": list(outcome.details), "usage": usage}, ensure_ascii=False, indent=1))
    if outcome.action == "patched" and args.out:
        shutil.rmtree(args.out, ignore_errors=True)
        shutil.copytree(args.repo, args.out, ignore=shutil.ignore_patterns(".git", "node_modules"))
        for path, content in outcome.files.items():
            target = args.out / path
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        print(f"→ {args.out}")
    return 0 if outcome.action == "patched" else 1


if __name__ == "__main__":
    sys.exit(main())
