"""패치·권장값·승인 경로가 공유하는 Kubernetes 리소스 상한 검사."""

from decimal import Decimal

from review_ai.spec.deploy_spec import Resources
from review_ai.static_check.context import size_mi


def check_resource_limits(resources: Resources) -> None:
    """선언된 상한은 요청량 이상이어야 한다. 상한 생략 자체는 기존 승인 정책에 맡긴다."""
    def cpu(quantity: str) -> Decimal:
        return Decimal(quantity[:-1]) / 1000 if quantity.endswith("m") else Decimal(quantity)

    for kind, quantity in (("cpu", cpu), ("memory", size_mi)):
        request = getattr(resources, f"{kind}_request")
        limit = getattr(resources, f"{kind}_limit")
        if limit is not None and quantity(limit) < quantity(request):
            raise ValueError(f"/runtime/resources/{kind}_limit ({limit})은 {kind}_request ({request}) 이상이어야 한다")
