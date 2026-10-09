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
- 같은 알림이 여러 번 온다. 같은 (검토, kind, 이미지 태그)는 한 번만 기록한다.
- baseline 은 앞으로만 간다. 같은 병합이면 다시 쓰지 않고(database_has_data 유지), 더 최근 검토의 baseline 은
  늦게 온 옛 알림으로 되돌리지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from review_api.cases import record_degraded_case
from review_common.repository import ReviewRepository

KINDS = {"Healthy": "healthy", "Degraded": "degraded"}


class ArgoCdEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app: str = Field(min_length=1)
    env: Literal["aws", "gcp", "local"]
    health: str
    images: list[str] = []
    revision: str | None = None

    def image_tags(self) -> list[str]:
        """repo:tag 와 repo@sha256:... 모두 받는다. 다이제스트만 있는 이미지는 태그가 없다."""
        tags = []
        for image in self.images:
            name = image.split("@", 1)[0]
            last = name.rsplit("/", 1)[-1]
            if ":" in last:
                tags.append(last.rsplit(":", 1)[1])
        return tags


async def latest_matching_review(repo: ReviewRepository, event: ArgoCdEvent) -> tuple[dict[str, Any], str] | None:
    """images 의 모든 태그로 검토를 찾아 가장 최근 검토와 그 태그. images 순서와 무관하다."""
    found: dict[str, tuple[dict[str, Any], str]] = {}
    for tag in event.image_tags():
        row = await repo.find_by_merge_sha(app=event.app, target_env=event.env, image_tag=tag)
        if row is not None:
            found.setdefault(row["review_id"], (row, tag))
    if not found:
        return None
    return max(found.values(), key=lambda match: match[0]["created_at"])


async def should_update_baseline(repo: ReviewRepository, row: dict[str, Any]) -> bool:
    current = await repo.get_baseline(row["app"], row["target_env"])
    if current is None:
        return True
    if current["merge_sha"] == row["merge_sha"]:
        return False  # 같은 배포 — 다시 쓰면 database_has_data 가 NULL 로 돌아간다
    owner = await repo.find_by_merge_sha_exact(current["merge_sha"]) if current["merge_sha"] else None
    return owner is None or owner["created_at"] <= row["created_at"]  # 더 최근 배포의 baseline 은 되돌리지 않는다


async def handle_deploy_event(repo: ReviewRepository, event: ArgoCdEvent) -> dict[str, Any]:
    kind = KINDS.get(event.health)
    if kind is None:
        return {"ignored": f"health {event.health}"}
    match = await latest_matching_review(repo, event)
    if match is None:
        return {"ignored": "이미지 태그와 맞는 병합 SHA 가 없다"}
    row, tag = match
    if await repo.has_deploy_event(review_id=row["review_id"], kind=kind, image_tag=tag):
        return {"review_id": row["review_id"], "duplicate": True}
    await repo.add_deploy_event(review_id=row["review_id"], app=event.app, target_env=event.env,
                                kind=kind, image_tag=tag, payload=event.model_dump(mode="json"))
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
