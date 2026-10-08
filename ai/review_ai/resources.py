"""패키지 데이터(catalog/·knowledge/) 위치.

소스 트리에서 실행하거나 editable 설치(`pip install -e ai`)면 `ai/<이름>` 을,
wheel 설치(`pip install ./ai`)면 빌드 때 패키지 안에 복사한 `review_ai/_data/<이름>` 을 쓴다.
"""

from __future__ import annotations

from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent


def data_dir(name: str) -> Path:
    candidates = (_PACKAGE_DIR / "_data" / name, _PACKAGE_DIR.parent / name)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"{name}/ 을 찾지 못했다: {', '.join(map(str, candidates))}")
