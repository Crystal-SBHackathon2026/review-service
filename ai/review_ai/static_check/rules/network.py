from __future__ import annotations

import ipaddress

from review_ai.static_check.context import CheckContext, Hit


def public_without_tls(ctx: CheckContext) -> list[Hit]:
    """NET-001 — 공개 진입점에 TLS 가 없다 (low)."""
    ingress = ctx.spec.network.ingress
    if ingress is None or not ingress.public or ingress.tls:
        return []
    return [Hit("/network/ingress/tls", "public: true, tls: false")]


def is_full_range(cidr: str) -> bool:
    return ipaddress.ip_network(cidr, strict=False).prefixlen == 0


def internal_open_to_all(ctx: CheckContext) -> list[Hit]:
    """NET-002 — 내부 전용(public false)이라 선언했는데 allowed_cidrs 에 전체 대역이 있다."""
    ingress = ctx.spec.network.ingress
    if ingress is None or ingress.public:
        return []
    return [
        Hit(f"/network/ingress/allowed_cidrs/{i}", f"public: false 인데 {cidr} 허용")
        for i, cidr in enumerate(ingress.allowed_cidrs)
        if is_full_range(cidr)
    ]
