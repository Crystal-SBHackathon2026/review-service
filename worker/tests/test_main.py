"""워커 진입점 — judge LLM 클라이언트 설정."""

from __future__ import annotations

import pytest

from review_worker.graph import JUDGE_MAX_RETRIES
from review_worker.main import make_llm

WORST_CASE_SECONDS = 10 * 60  # SDK 기본값(600초·재시도 2번)이면 한 검토가 judge 에서 최대 12×600초 멈췄다


def test_judge_llm_is_bounded_and_retried_only_by_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    client = make_llm()._inner._client  # type: ignore[union-attr]

    assert client.max_retries == 0  # 재시도는 그래프 judge 노드(JUDGE_MAX_RETRIES) 한 곳에서만
    assert client.timeout * (JUDGE_MAX_RETRIES + 1) < WORST_CASE_SECONDS
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert make_llm() is None
