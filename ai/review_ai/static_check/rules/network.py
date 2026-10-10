from __future__ import annotations

import ipaddress

from review_ai.static_check.context import CheckContext, Hit


def public_without_tls(ctx: CheckContext) -> list[Hit]:
    """NET-001 — 공개 진입점에 TLS 가 없다 (low)."""
    ingress = ctx.spec.network.ingress
    if ctx.spec.network.service is not None:
        return [Hit("/network/service/type", "공개 LoadBalancer Service는 HTTP 80을 제공하며 TLS를 종료하지 않는다")]
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


def local_internal_not_enforced(ctx: CheckContext) -> list[Hit]:
    """NET-003 — local 에서 public: false 를 overlay 가 강제하지 못한다 (low 경고).

    local overlay 는 public 값과 무관하게 같은 Traefik Ingress 를 만든다. 내부 전용인지는 클러스터 밖
    (터널 ingress·라우터 포트포워딩·NodePort·VLAN)이 정한다. allowed_cidrs 가 있으면 INGRESS_CIDRS_NOT_ENFORCED 가
    커밋을 막으므로 여기서는 보지 않는다.
    """
    ingress = ctx.spec.network.ingress
    if ingress is None or ingress.public or ingress.allowed_cidrs:
        return []
    return [Hit("/network/ingress/public", "local: public false 지만 Traefik Ingress 는 공개와 같은 모양 — 내부 전용은 클러스터 밖에서 지켜야 한다")]
