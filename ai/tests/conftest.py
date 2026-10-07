from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

AI_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = AI_ROOT / "samples"


def load_sample_dict(name: str) -> dict[str, Any]:
    return yaml.safe_load((SAMPLES / name).read_text(encoding="utf-8"))


def load_cases() -> list[dict[str, Any]]:
    return yaml.safe_load((SAMPLES / "cases.yaml").read_text(encoding="utf-8"))["cases"]


@pytest.fixture
def sample_app() -> dict[str, Any]:
    """01 샘플 — 대부분의 단위 테스트가 이걸 복사해 한 필드만 바꾼다."""
    return copy.deepcopy(load_sample_dict("01-pass-sample-app-aws.yaml"))
