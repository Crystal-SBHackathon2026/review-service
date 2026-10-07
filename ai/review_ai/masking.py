"""mask_spec() — Kafka 에 싣기 전, 프롬프트에 넣기 전에 비밀로 보이는 값을 가린다.

- runtime.env(및 baseline 안의 env): 이름이나 값이 비밀처럼 보이면 값의 타입과 무관하게 가린다
- 그 밖의 모든 문자열: 값이 비밀처럼 보이면 가린다 (image.tag·ingress.host 같은 자유 텍스트 필드 포함)
판별 기준은 SEC-001 과 같다 (secrets_pattern). 입력은 바꾸지 않고 새 dict 를 돌려준다.
"""

from __future__ import annotations

from typing import Any

from review_ai.secrets_pattern import MASK, has_secret_value, looks_secret


def _mask_env(env: dict[str, Any]) -> dict[str, Any]:
    return {
        name: MASK if not isinstance(value, dict | list) and looks_secret(name, value) else _mask(value)
        for name, value in env.items()
    }


def _mask(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _mask_env(value) if key == "env" and isinstance(value, dict) else _mask(value)
                for key, value in node.items()}
    if isinstance(node, list | tuple):
        return [_mask(item) for item in node]
    if isinstance(node, str) and has_secret_value(node):
        return MASK
    return node


def mask_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return _mask(spec)
