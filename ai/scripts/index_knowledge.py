"""knowledge/ 를 Qdrant 에 색인한다 — 로컬 개발용 진입점. 본체는 review_ai.retrieval.indexer (이미지에서도 같은 코드).

    docker compose up -d qdrant
    .venv/bin/python scripts/index_knowledge.py --qdrant-url http://localhost:6333

S3 review-docs 버킷을 받아 색인할 때는 내려받은 디렉터리를 --knowledge-dir 로 준다(버킷 경로 = knowledge/ 경로).

    aws s3 sync s3://<bucket>/ /tmp/knowledge && ... --knowledge-dir /tmp/knowledge
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review_ai.retrieval.indexer import main  # noqa: E402

if __name__ == "__main__":
    main()
