"""deploy_spec 하나를 gitops overlay 로 렌더링해 출력하거나 디렉터리에 쓴다.

    .venv/bin/python scripts/render_overlay.py samples/01-pass-sample-app-aws.yaml
    .venv/bin/python scripts/render_overlay.py samples/01-pass-sample-app-aws.yaml --out ../../gitops
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review_ai.overlay import render_overlay  # noqa: E402
from review_ai.spec.deploy_spec import load_spec  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec")
    parser.add_argument("--out", default=None, help="gitops 레포 루트. 주면 apps/<앱>/overlays/<환경>/ 에 쓴다")
    args = parser.parse_args()
    rendered = render_overlay(load_spec(args.spec))
    for filename, content in rendered.files.items():
        if args.out:
            target = Path(args.out) / rendered.directory / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            print(f"wrote {target}")
        else:
            print(f"--- {rendered.directory}/{filename}\n{content}")
    for warning in rendered.warnings:
        print(f"경고: {warning}", file=sys.stderr)


if __name__ == "__main__":
    main()
