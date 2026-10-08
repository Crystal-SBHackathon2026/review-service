"""렌더러 경고 — 커밋 단계(commit_overlay)가 문장을 파싱하지 않고 진행·중단을 고르도록 code·blocking 을 붙인다.

RenderWarning 은 str 이라 기존처럼 문자열로 출력·검색해도 된다. 판단은 code·blocking 으로 한다.
blocking=True 는 "overlay 만 커밋하면 배포가 깨지거나 명세가 요구한 보호가 빠진다" 는 뜻이다 → 커밋하지 말고 blocked 로 돌려준다.
"""

from __future__ import annotations

from typing import Literal

WarningCode = Literal[
    "SECRET_KEYS_REQUIRED",
    "DB_PROVISIONING_REQUIRED",
    "BUCKET_PROVISIONING_REQUIRED",
    "VOLUME_UNSUPPORTED",
    "INGRESS_CIDRS_NOT_ENFORCED",
    "TLS_HOST_MISSING",
    "TLS_SECRET_REQUIRED",
]

# 코드별 중단 여부. 근거는 Notion "배포 에이전트 설계" 5절 + 보호 누락은 fail-closed.
BLOCKING: dict[str, bool] = {
    "SECRET_KEYS_REQUIRED": False,  # 없으면 Rollout 이 시작 단계에서 멈추고 자동 롤백된다
    "DB_PROVISIONING_REQUIRED": True,  # 없는 DB 를 바라보면 앱이 뜨지 않는다
    "BUCKET_PROVISIONING_REQUIRED": True,
    "VOLUME_UNSUPPORTED": True,  # PVC 가 Pending 에 머문다
    # 명세가 막으라고 한 대역이 강제되지 않아 공개로 열린다(local·gcp 만. aws 는 ALB inbound-cidrs 로 강제된다).
    # TLS_HOST_MISSING 과 같은 보호 누락이라 fail-closed (D11, 10/08).
    "INGRESS_CIDRS_NOT_ENFORCED": True,
    "TLS_HOST_MISSING": True,  # TLS 를 요구했는데 인증서를 못 붙여 평문으로 열린다
    "TLS_SECRET_REQUIRED": False,  # 인증서 Secret 이 없으면 Ingress 컨트롤러가 기본 인증서를 쓴다 — 노출은 없음
}


class RenderWarning(str):
    """문자열 = 사람이 읽는 설명. code·blocking 은 코드가 읽는 값."""

    code: WarningCode
    blocking: bool

    def __new__(cls, code: WarningCode, message: str) -> RenderWarning:
        obj = super().__new__(cls, message)
        obj.code = code
        obj.blocking = BLOCKING[code]
        return obj

    def __reduce__(self) -> tuple[type, tuple[str, str]]:
        return (RenderWarning, (self.code, str(self)))

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "blocking": self.blocking, "message": str(self)}
