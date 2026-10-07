"""비밀 값 판별 기준 — SEC-001, mask_spec(), 패치 검증, LLM 출력 검사가 모두 같은 기준을 쓴다.

여러 곳이 다른 기준을 쓰면 "SEC-001 은 잡았는데 Kafka 에는 평문이 실려 간다" 같은 틈이 생긴다.
- 이름 기준: env 이름을 '_' 로 나눈 토큰에 비밀을 뜻하는 단어가 있다 (BYPASS·PASSPORT 같은 오탐을 피하려고 토큰 단위)
- 값 기준: 접속 문자열의 비밀번호, 알려진 토큰 접두, PEM 키 — 어느 필드에 있든 잡는다
- 이미 가린 값(MASK)도 비밀로 본다. 가린 명세를 다시 검사해도 SEC-001 이 사라지지 않게
"""

from __future__ import annotations

import re

MASK = "***MASKED***"

SECRET_TOKENS = frozenset({
    "PASSWORD", "PASSWD", "PASS", "PWD", "SECRET", "SECRETS", "TOKEN", "CREDENTIAL", "CREDENTIALS",
    "APIKEY", "DSN", "AUTHORIZATION", "BEARER",
})  # AUTH·SESSION·COOKIE 는 AUTH_ENABLED·SESSION_TIMEOUT 같은 설정값 오탐이 많아 뺐다 (값 패턴이 Bearer 를 잡는다)
SECRET_SUFFIXES = ("API_KEY", "PRIVATE_KEY", "ACCESS_KEY", "SIGNING_KEY", "SECRET_KEY", "_KEY")

VALUE_PATTERNS = (
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s:@/]*:[^\s@]+@", re.IGNORECASE),  # scheme://[user]:password@
    re.compile(r"^[^\s:@/]+:[^\s@/]+@[^\s@]+$"),  # user:password@host (scheme 없음)
    re.compile(r"[?&;](password|passwd|pwd|secret|token|api_key)=[^&\s]+", re.IGNORECASE),
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),  # OpenAI·Anthropic 형식
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"^\s*Bearer\s+\S+", re.IGNORECASE),
)


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return bool(SECRET_TOKENS & set(upper.split("_"))) or upper.endswith(SECRET_SUFFIXES)


def has_secret_value(value: str) -> bool:
    return MASK in value or any(p.search(value) for p in VALUE_PATTERNS)


def looks_secret(name: str, value: object) -> bool:
    if is_secret_name(name):
        return True
    return isinstance(value, str) and has_secret_value(value)


def redact(text: str) -> str:
    """자유 텍스트(LLM 설명 등) 안의 비밀처럼 보이는 부분을 가린다."""
    for pattern in VALUE_PATTERNS:
        text = pattern.sub(MASK, text)
    return text
