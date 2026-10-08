#!/usr/bin/env bash
# knowledge/ 를 S3 review-docs 버킷(terraform infra/storage, oneaction-review-docs-<계정ID>)에 올린다.
# 버킷 경로 = knowledge/ 경로 (rules/{any,aws,gcp,local}/ · incidents/ · guides/).
#
#   scripts/sync_knowledge_s3.sh <버킷>            # dry-run: 무엇이 바뀌는지만 보여 준다
#   scripts/sync_knowledge_s3.sh <버킷> --apply    # 실제 업로드
#
# *.md 만 다룬다. 로컬에서 지운 문서는 버킷에서도 지운다(버킷 versioning 이 켜져 있어 되돌릴 수 있다).
# terraform 이 만든 접두사 자리표시 객체(rules/aws/ 등)는 필터 밖이라 건드리지 않는다.
set -euo pipefail

usage() { echo "usage: $0 <bucket> [--apply]" >&2; exit 2; }

[[ $# -ge 1 && $# -le 2 ]] || usage
bucket="$1"
mode="${2:-}"
[[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || { echo "버킷 이름 형식이 아니다: $bucket" >&2; exit 2; }
[[ -z "$mode" || "$mode" == "--apply" ]] || usage

ai_dir="$(cd "$(dirname "$0")/.." && pwd)"

# 올리기 전에 색인기와 같은 파서로 front matter 를 검사한다 — 깨진 문서가 버킷에 들어가지 않게.
"$ai_dir/.venv/bin/python" - "$ai_dir/knowledge" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
from review_ai.retrieval.knowledge import load_chunks
chunks = load_chunks(Path(sys.argv[1]))
if not chunks:
    sys.exit("knowledge/ 에 문서가 없다")
print(f"문서 검사 통과: 청크 {len(chunks)}개")
PY

args=(s3 sync "$ai_dir/knowledge/" "s3://$bucket/" --delete --exclude "*" --include "*.md" --content-type "text/markdown; charset=utf-8")
if [[ "$mode" != "--apply" ]]; then
  args+=(--dryrun)
  echo "[dry-run] 실제로 올리려면 --apply"
fi
aws "${args[@]}"
