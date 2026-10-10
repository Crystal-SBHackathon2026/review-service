"""Argo CD Notifications 웹훅 — 배포 결과(Healthy·Degraded)를 검토에 잇는다.

페이로드 (성진님 Notifications 템플릿, 10/09 14:56 부터 실제 수신. trigger oncePer = sync revision, 폴링 30초)

    {
      "app": "sample-app",                      # 앱 이름 (deploy_spec metadata.name)
      "env": "aws",                             # aws | gcp | local
      "health": "Healthy",                      # .app.status.health.status
      "images": ["ghcr.io/crystal-sbhackathon2026/sample-app:3c7ad2e…",    # .app.status.summary.images
                 "ghcr.io/crystal-sbhackathon2026/sample-app:c8cf4ec…"],
      "revision": "<gitops 커밋 SHA>"           # .app.status.sync.revision
    }

- 이미지 태그는 앱 레포 병합 커밋 SHA(40자)다. 그 태그를 merge_sha 로 가진 검토의 배포로 본다.
- **images 에는 여러 개가 올 수 있고 순서는 보장되지 않는다.** 롤아웃 중이면 옛 이미지와 새 이미지가 같이 온다.
  그래서 모든 태그로 검토를 찾고 가장 최근 검토(created_at)를 고른다. 첫 매칭을 쓰면 새 배포를 옛 검토에 기록한다
  (10/09 05:45:18 실제 사례).
- 같은 알림이 여러 번 온다. 그 (검토, 환경)의 마지막 기록과 kind·이미지 태그가 같으면 다시 기록하지 않는다.
  상태가 바뀐 알림(Healthy → Degraded → Healthy)은 같은 내용이 전에 왔어도 남긴다 — 마지막 알림이 지금 상태다.
- 검토 한 건은 대상 환경 하나다(deploy.yaml). 같은 병합을 다른 환경(aws 검토의 local 배포)도 배포하는데,
  그 env 로 찾은 검토가 없으면 env 를 보지 않고 같은 앱·병합 SHA 검토를 찾아 deploy_events 에 그 env 로 남긴다
  (cross_env). 진행 화면의 환경별 카드용이다 — baseline·degraded 판단 사례는 검토 대상 환경에서만 남긴다.
- baseline 은 앞으로만 간다. 같은 병합이면 다시 쓰지 않고(database_has_data 유지), 더 최근 검토의 baseline 은
  늦게 온 옛 알림으로 되돌리지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from review_api.deployment_contract import ArgoCdEvent, safe_data

from review_api.cases import record_degraded_case
from review_common.repository import ReviewRepository

KINDS = {"Healthy": "healthy", "Degraded": "degraded"}
FAILURE_KINDS = frozenset({"health_degraded", "sync_failed"})


async def latest_matching_review(repo: ReviewRepository, event: ArgoCdEvent, *,
                                 any_env: bool = False) -> tuple[dict[str, Any], str] | None:
    """images 의 모든 태그로 검토를 찾아 가장 최근 검토와 그 태그. images 순서와 무관하다.

    any_env=True 면 검토의 대상 환경을 보지 않는다 (다른 환경 배포 알림)."""
    found: dict[str, tuple[dict[str, Any], str]] = {}
    for tag in event.image_tags():
        row = await repo.find_by_merge_sha(app=event.app, target_env=None if any_env else event.env, image_tag=tag)
        if row is not None:
            found.setdefault(row["review_id"], (row, tag))
    if not found:
        return None
    return max(found.values(), key=lambda match: match[0]["created_at"])


async def should_update_baseline(repo: ReviewRepository, row: dict[str, Any]) -> bool:
    if await repo.is_deployment_failing(row["review_id"]):  # 실패 뒤 회복했으면 갱신한다
        return False
    current = await repo.get_baseline(row["app"], row["target_env"])
    if current is None:
        return True
    if current["merge_sha"] == row["merge_sha"]:
        return bool(current.get("derived"))  # 같은 배포 — 다시 쓰면 database_has_data 가 NULL 로 돌아간다
    owner = await repo.find_by_merge_sha_exact(current["merge_sha"]) if current["merge_sha"] else None
    return owner is None or owner["created_at"] <= row["created_at"]  # 더 최근 배포의 baseline 은 되돌리지 않는다


async def handle_legacy_event(repo: ReviewRepository, event: ArgoCdEvent) -> dict[str, Any]:
    kind = KINDS.get(event.health)
    if kind is None:
        return {"ignored": f"health {event.health}"}
    match = await latest_matching_review(repo, event)
    cross_env = match is None
    if cross_env:
        match = await latest_matching_review(repo, event, any_env=True)
    if match is None:
        return {"ignored": "이미지 태그와 맞는 병합 SHA 가 없다"}
    row, tag = match
    last = await repo.last_deploy_event(review_id=row["review_id"], target_env=event.env)
    if last is not None and (last["kind"], last["image_tag"]) == (kind, tag):
        return {"review_id": row["review_id"], "duplicate": True}
    await repo.add_deploy_event(review_id=row["review_id"], app=event.app, target_env=event.env,
                                kind=kind, image_tag=tag, payload=safe_data(event.model_dump(mode="json")))
    if cross_env:  # 검토 대상이 아닌 환경 — 기록만. baseline·사례는 그 환경의 검토가 생겼을 때 그 검토로
        return {"review_id": row["review_id"], "recorded": kind, "cross_env": True}
    result: dict[str, Any] = {"review_id": row["review_id"], "recorded": kind}
    if kind == "healthy" and row.get("final_spec"):
        if await should_update_baseline(repo, row):
            await repo.upsert_baseline(app=event.app, target_env=event.env, spec=row["final_spec"],
                                       spec_ref=row["spec_ref"], merge_sha=row["merge_sha"],
                                       observed_at=datetime.now(UTC))
            result["baseline"] = "updated"
        else:
            result["baseline"] = "kept"
    if kind == "degraded":
        await record_degraded_case(repo, row)
    return result


def is_verified_success(event):
    return (event.kind() == "deployed" and event.health == "Healthy" and event.sync_status == "Synced"
            and event.operation is not None and event.operation.phase == "Succeeded")


async def match_versioned_event(repo, event):
    revision = event.operation.revision if event.operation else None
    if not revision and event.kind() == "health_degraded" and event.sync_status == "Synced":
        revision = event.revision
    if not revision and event.kind() == "deployed":
        revision = event.revision
    row = await repo.find_deployment_review(event.app, event.env, revision)
    # Successful notifications retain main's image-based matching. Argo can advance sync.revision
    # without a new operation, and CI's image commit differs from our overlay commit.
    if is_verified_success(event):
        match = await latest_matching_review(repo, event)
        if match:
            row = match[0]
    if row is None:
        row = await repo.find_deployment_review(event.app, None, revision)
    if row is None and is_verified_success(event):
        match = await latest_matching_review(repo, event, any_env=True)
        row = match[0] if match else None
    if row is None and event.kind() in FAILURE_KINDS:
        row = await failure_image_match(repo, event)
    if row and event.namespace:
        target = (row.get("final_spec") or {}).get("target") or {}
        if event.namespace != (target.get("namespace") or event.app):
            return None
    return row


async def failure_image_match(repo, event):
    """실패 알림(health_degraded·sync_failed)의 이미지 태그 → merge_sha 대체 매칭.

    PR 하나에 gitops 리비전이 두 번 바뀐다 (overlay 커밋 → CI 이미지 태그 커밋). 두 번째 리비전의 실패는 revision 이
    검토의 gitops_commit_sha 와 달라 revision 으로 못 찾는다 (rv_20261010_fb27c6bf: overlay f316634 → 이미지 f817924).
    롤아웃 중 images 에는 옛 stable 태그도 같이 오므로, 고른 검토가 그 앱·환경에서 가장 최근에 병합한 검토일 때만 잇는다
    — 새 태그가 없어 옛 검토만 맞으면 옛 stable 이 새 실패를 떠안는다."""
    for any_env in (False, True):
        match = await latest_matching_review(repo, event, any_env=any_env)
        if match:
            row = match[0]
            newest = await repo.latest_merged_review(row["app"], row["target_env"])
            return row if newest is not None and newest["review_id"] == row["review_id"] else None
    return None


async def store_observation(repo, event, row):
    event_id, attempt_key = event.identifiers()
    occurred = event.observed_at or (event.operation.finished_at if event.operation else None) or datetime.now(UTC)
    payload = safe_data(event.model_dump(mode="json"))
    observation = dict(event_id=event_id, attempt_key=attempt_key, app=event.app, target_env=event.env,
                       kind=event.kind(), review_id=row["review_id"] if row else None,
                       repository=row["spec_ref"].get("repository") if row else None,
                       spec_snapshot=safe_data(row.get("final_spec")) if row else None,
                       payload=payload, occurred_at=occurred)
    inserted = await repo.record_deployment(observation)
    return event_id, inserted


async def handle_deploy_event(repo, event):
    if not event.schema_version and event.kind() != "sync_failed":
        # Preserve the legacy response while retaining failure evidence for the new worker.
        result = await handle_legacy_event(repo, event)
        if result.get("review_id") and not result.get("cross_env"):
            row = await repo.get_review(result["review_id"])
            if row["target_env"] != event.env:
                return result
            await store_observation(repo, event, row)
            if result.get("duplicate") and event.health == "Healthy" and row.get("final_spec"):
                if await should_update_baseline(repo, row):
                    await repo.upsert_baseline(app=event.app, target_env=event.env, spec=row["final_spec"],
                                               spec_ref=row["spec_ref"], merge_sha=row["merge_sha"], observed_at=datetime.now(UTC))
        elif event.kind() == "health_degraded" and "ignored" in result:
            event_id, _ = await store_observation(repo, event, None)
            result["event_id"] = event_id
        return result
    row = await match_versioned_event(repo, event)
    cross_env = row is not None and row["target_env"] != event.env
    event_id, inserted = await store_observation(repo, event, None if cross_env else row)
    result = dict(event_id=event_id, review_id=row["review_id"] if row else None,
                  recorded=event.kind(), duplicate=not inserted, linked=row is not None and not cross_env)
    if cross_env:
        result["cross_env"] = True
    # Healthy may describe the old stable workload while the new sync failed.
    success = is_verified_success(event)
    if row and (success or event.kind() != "deployed"):
        kind = {"deployed": "healthy", "health_degraded": "degraded", "sync_failed": "sync_failed"}[event.kind()]
        tag = row.get("merge_sha")
        payload = safe_data(event.model_dump(mode="json"))
        last = await repo.last_deploy_event(review_id=row["review_id"], target_env=event.env)
        await repo.mirror_deployment_event(event_id=event_id, review_id=row["review_id"], app=event.app,
                                           target_env=event.env, kind=kind, image_tag=tag, payload=payload)
        if not inserted and last is not None and last["kind"] != kind:
            # 전에 받은 것과 같은 본문(같은 event_id)이 상태 전이로 다시 왔다 — 회복·재실패를 남긴다
            await repo.add_deploy_event(review_id=row["review_id"], app=event.app, target_env=event.env,
                                        kind=kind, image_tag=tag, payload=payload)
    if success and row and not cross_env and row.get("final_spec"):
        if await should_update_baseline(repo, row):
            await repo.upsert_baseline(app=event.app, target_env=event.env, spec=row["final_spec"],
                                      spec_ref=row["spec_ref"], merge_sha=row["merge_sha"], observed_at=datetime.now(UTC))
            result["baseline"] = "updated"
        else:
            result["baseline"] = "kept"
    return result
