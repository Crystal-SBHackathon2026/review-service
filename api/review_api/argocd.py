"""Argo CD Notifications 웹훅 페이로드.

⚠️ 성진님 Notifications 템플릿이 확정되기 전까지 아래 형식을 **가정**한다. 템플릿이 정해지면 이 모델만 맞춘다.

    {
      "app": "sample-app",                      # 앱 이름 (deploy_spec metadata.name)
      "env": "aws",                             # aws | gcp | local
      "health": "Healthy",                      # .app.status.health.status
      "images": ["ghcr.io/crystal-sbhackathon2026/sample-app:b084c24"],   # .app.status.summary.images
      "revision": "<gitops 커밋 SHA>"           # .app.status.sync.revision (선택)
    }

이미지 태그가 그 검토의 merge_sha(짧은 SHA 면 앞부분)와 같을 때만 그 검토의 배포로 본다.
병합 직후 ① 새 설정 + 옛 이미지 ② 새 설정 + 새 이미지로 두 번 배포되는데, ① 은 이미지 태그가 달라 걸러진다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
