"""Argo CD 배포 알림 — 10/09 운영 DB deploy_events 에서 실제로 생긴 문제를 그대로 재현한다.

수신(UTC)  images (순서 그대로)                 맞는 검토
05:44:59   [31b77be]                             31b77be 검토
05:45:18   [31b77be(옛), c8cf4ec(새)]            c8cf4ec 검토  ← 예전 코드는 첫 매칭인 31b77be 검토에 기록했다
05:53:14   [3c7ad2e(새), c8cf4ec(옛)]            3c7ad2e 검토
"""

from __future__ import annotations

from typing import Any

import pytest

from review_api.argocd import ArgoCdEvent, handle_deploy_event
from review_common.repository import InMemoryReviewRepository

REPO = "Crystal-SBHackathon2026/sample-app"
IMAGE = "ghcr.io/crystal-sbhackathon2026/sample-app"
SHA = {"31b77be": "31b77bef5a78d4273b6e3d541818687e761194b0",
       "c8cf4ec": "c8cf4ec3f1d8cf5d225345498dbc4192751ef2da",
       "3c7ad2e": "3c7ad2e366ac735ad040419f49ce8df216d44e27"}
REVIEW = {"31b77be": "rv_20261009_9f078914", "c8cf4ec": "rv_20261009_ebaeb281", "3c7ad2e": "rv_20261009_53c71e3c"}


async def merged_review(repo: InMemoryReviewRepository, short: str) -> str:
    """병합까지 끝난 검토 — 만든 순서가 created_at 순서다 (31b77be → c8cf4ec → 3c7ad2e)."""
    rid = REVIEW[short]
    ref = {"repository": REPO, "commit": short * 1, "path": "deploy.yaml"}
    await repo.insert_review(review_id=rid, app="sample-app", target_env="aws", repo_id=REPO, spec_ref=ref,
                             pr_head_sha=short, requested_by="t")
    await repo.update_review(rid, status="committed", merge_sha=SHA[short],
                             final_spec={"metadata": {"name": "sample-app"}, "from": short})
    return rid


@pytest.fixture
async def repo() -> InMemoryReviewRepository:
    r = InMemoryReviewRepository()
    for short in ("31b77be", "c8cf4ec", "3c7ad2e"):
        await merged_review(r, short)
    return r


def event(*shorts: str, health: str = "Healthy") -> ArgoCdEvent:
    return ArgoCdEvent(app="sample-app", env="aws", health=health,
                       images=[f"{IMAGE}:{SHA[s]}" for s in shorts], revision="0f94208")


def recorded(repo: InMemoryReviewRepository) -> list[tuple[str, str, str]]:
    return [(e["review_id"], e["kind"], e["image_tag"][:7]) for e in repo.deploy_events]


def baseline_from(repo: InMemoryReviewRepository) -> str:
    return repo.baselines[("sample-app", "aws")]["spec"]["from"]


async def test_real_10_09_sequence(repo: InMemoryReviewRepository) -> None:
    await handle_deploy_event(repo, event("31b77be"))
    second = await handle_deploy_event(repo, event("31b77be", "c8cf4ec"))
    await handle_deploy_event(repo, event("3c7ad2e", "c8cf4ec"))

    assert second["review_id"] == REVIEW["c8cf4ec"]  # 새 배포는 새 검토에
    assert recorded(repo) == [(REVIEW["31b77be"], "healthy", "31b77be"),
                              (REVIEW["c8cf4ec"], "healthy", "c8cf4ec"),
                              (REVIEW["3c7ad2e"], "healthy", "3c7ad2e")]
    assert baseline_from(repo) == "3c7ad2e"


@pytest.mark.parametrize("images", [("31b77be", "c8cf4ec"), ("c8cf4ec", "31b77be")])
async def test_image_order_does_not_matter(repo: InMemoryReviewRepository, images: tuple[str, ...]) -> None:
    result = await handle_deploy_event(repo, event(*images))

    assert result == {"review_id": REVIEW["c8cf4ec"], "recorded": "healthy", "baseline": "updated"}
    assert baseline_from(repo) == "c8cf4ec"


async def test_same_alert_twice_records_once(repo: InMemoryReviewRepository) -> None:
    first = await handle_deploy_event(repo, event("3c7ad2e", "c8cf4ec"))
    repo.baselines[("sample-app", "aws")]["database_has_data"] = True  # 관측한 사실
    again = await handle_deploy_event(repo, event("3c7ad2e", "c8cf4ec"))

    assert first["recorded"] == "healthy"
    assert again == {"review_id": REVIEW["3c7ad2e"], "duplicate": True}
    assert len(repo.deploy_events) == 1
    assert repo.baselines[("sample-app", "aws")]["database_has_data"] is True  # 다시 쓰지 않아 NULL 로 안 돌아간다


async def test_same_merge_new_tag_keeps_baseline(repo: InMemoryReviewRepository) -> None:
    """같은 검토의 baseline 이 이미 있으면 다시 쓰지 않는다 (database_has_data 유지)."""
    await handle_deploy_event(repo, event("3c7ad2e"))
    repo.baselines[("sample-app", "aws")]["database_has_data"] = False
    result = await handle_deploy_event(repo, ArgoCdEvent(app="sample-app", env="aws", health="Healthy",
                                                         images=[f"{IMAGE}:{SHA['3c7ad2e'][:7]}"]))

    assert result["baseline"] == "kept"
    assert repo.baselines[("sample-app", "aws")]["database_has_data"] is False


async def test_late_old_alert_does_not_roll_back_baseline(repo: InMemoryReviewRepository) -> None:
    await handle_deploy_event(repo, event("3c7ad2e"))
    late = await handle_deploy_event(repo, event("c8cf4ec"))  # 옛 배포 알림이 늦게 왔다

    assert late == {"review_id": REVIEW["c8cf4ec"], "recorded": "healthy", "baseline": "kept"}
    assert baseline_from(repo) == "3c7ad2e"


async def test_degraded_uses_latest_review_and_skips_duplicates(repo: InMemoryReviewRepository,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    cases: list[str] = []

    async def fake_record(_repo: Any, row: dict[str, Any]) -> str:
        cases.append(row["review_id"])
        return row["review_id"]

    monkeypatch.setattr("review_api.argocd.record_degraded_case", fake_record)
    await handle_deploy_event(repo, event("31b77be", "c8cf4ec", health="Degraded"))
    dup = await handle_deploy_event(repo, event("c8cf4ec", "31b77be", health="Degraded"))

    assert cases == [REVIEW["c8cf4ec"]]
    assert dup == {"review_id": REVIEW["c8cf4ec"], "duplicate": True}
    assert recorded(repo) == [(REVIEW["c8cf4ec"], "degraded", "c8cf4ec")]
    assert ("sample-app", "aws") not in repo.baselines


async def test_unknown_images_and_health_are_ignored(repo: InMemoryReviewRepository) -> None:
    assert "ignored" in await handle_deploy_event(repo, ArgoCdEvent(app="sample-app", env="aws", health="Healthy",
                                                                    images=[f"{IMAGE}:{'0' * 40}"]))
    assert "ignored" in await handle_deploy_event(repo, event("3c7ad2e", health="Progressing"))
    assert repo.deploy_events == []


# --- 다른 환경 배포 알림 (cross_env) — aws 검토의 같은 병합을 로컬 Argo CD 도 배포한다 -------------------

def local_event(*shorts: str, health: str = "Healthy") -> ArgoCdEvent:
    return ArgoCdEvent(app="sample-app", env="local", health=health,
                       images=[f"{IMAGE}:{SHA[s]}" for s in shorts], revision="0f94208")


async def test_other_env_alert_is_recorded_without_baseline(repo: InMemoryReviewRepository) -> None:
    await handle_deploy_event(repo, event("3c7ad2e"))
    result = await handle_deploy_event(repo, local_event("3c7ad2e"))

    assert result == {"review_id": REVIEW["3c7ad2e"], "recorded": "healthy", "cross_env": True}
    assert [(e["review_id"], e["target_env"], e["kind"]) for e in repo.deploy_events] == [
        (REVIEW["3c7ad2e"], "aws", "healthy"), (REVIEW["3c7ad2e"], "local", "healthy")]
    assert ("sample-app", "local") not in repo.baselines  # local baseline 을 aws 검토로 만들지 않는다
    assert baseline_from(repo) == "3c7ad2e"


async def test_other_env_alert_does_not_touch_target_baseline(repo: InMemoryReviewRepository) -> None:
    await handle_deploy_event(repo, event("3c7ad2e"))
    repo.baselines[("sample-app", "aws")]["database_has_data"] = True
    await handle_deploy_event(repo, local_event("c8cf4ec"))  # 로컬은 아직 옛 병합

    assert baseline_from(repo) == "3c7ad2e"
    assert repo.baselines[("sample-app", "aws")]["database_has_data"] is True


async def test_other_env_same_alert_twice_records_once(repo: InMemoryReviewRepository) -> None:
    await handle_deploy_event(repo, local_event("3c7ad2e"))
    again = await handle_deploy_event(repo, local_event("3c7ad2e"))

    assert again == {"review_id": REVIEW["3c7ad2e"], "duplicate": True}
    assert [(e["target_env"], e["kind"]) for e in repo.deploy_events] == [("local", "healthy")]


async def test_aws_and_local_healthy_are_not_duplicates(repo: InMemoryReviewRepository) -> None:
    """has_deploy_event 가 env 를 보지 않으면 먼저 온 local 때문에 aws Healthy 가 버려지고 baseline 도 안 바뀐다."""
    await handle_deploy_event(repo, local_event("3c7ad2e"))
    aws = await handle_deploy_event(repo, event("3c7ad2e"))

    assert aws == {"review_id": REVIEW["3c7ad2e"], "recorded": "healthy", "baseline": "updated"}
    assert len(repo.deploy_events) == 2


async def test_other_env_degraded_records_no_case(repo: InMemoryReviewRepository,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    cases: list[str] = []

    async def fake_record(_repo: Any, row: dict[str, Any]) -> str:
        cases.append(row["review_id"])
        return row["review_id"]

    monkeypatch.setattr("review_api.argocd.record_degraded_case", fake_record)
    result = await handle_deploy_event(repo, local_event("3c7ad2e", health="Degraded"))

    assert result == {"review_id": REVIEW["3c7ad2e"], "recorded": "degraded", "cross_env": True}
    assert cases == []


async def test_review_for_that_env_wins_over_cross_env(repo: InMemoryReviewRepository) -> None:
    """같은 병합을 local 대상으로 검토한 것이 있으면 그 검토에 기록하고 local baseline 을 갱신한다 (기존 경로)."""
    rid = "rv_20261009_local001"
    await repo.insert_review(review_id=rid, app="sample-app", target_env="local", repo_id=REPO,
                             spec_ref={"repository": REPO, "commit": "l" * 40, "path": "deploy.yaml"},
                             pr_head_sha="l" * 40, requested_by="t")
    await repo.update_review(rid, status="committed", merge_sha=SHA["3c7ad2e"], final_spec={"from": "local"})

    result = await handle_deploy_event(repo, local_event("3c7ad2e"))

    assert result == {"review_id": rid, "recorded": "healthy", "baseline": "updated"}
    assert repo.baselines[("sample-app", "local")]["spec"] == {"from": "local"}
