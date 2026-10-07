from __future__ import annotations

from review_ai.static_check.context import CheckContext, Hit


def public_without_tls(ctx: CheckContext) -> list[Hit]:
    """NET-001 — 공개 진입점에 TLS 가 없다 (low)."""
    ingress = ctx.spec.network.ingress
    if ingress is None or not ingress.public or ingress.tls:
        return []
    return [Hit("/network/ingress/tls", "public: true, tls: false")]
