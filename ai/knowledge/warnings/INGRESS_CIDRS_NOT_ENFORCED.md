---
warning_code: INGRESS_CIDRS_NOT_ENFORCED
doc_type: warning
provider: any
blocking: true
title: Ingress 허용 대역(allowed_cidrs)을 대상 환경에서 강제하지 못한다
---
## 왜 멈추나
명세가 network.ingress.allowed_cidrs 로 "이 대역에서만 열어라" 라고 했는데, 대상 환경의 overlay 로는 그 제한을 걸 수 없다.
그대로 커밋하면 Ingress 는 만들어지지만 제한 없이 **누구에게나** 열린다. 명세가 요구한 보호가 조용히 빠지는 것이라
TLS_HOST_MISSING 과 같이 중단(fail-closed)으로 둔다 (D11, 2026-10-08).

## 환경별
- aws: ALB 의 `alb.ingress.kubernetes.io/inbound-cidrs` 로 강제된다 — 이 경고가 나오지 않는다.
- gcp: GCE Ingress 에는 대역 제한 annotation 이 없다. Cloud Armor 보안 정책을 BackendConfig 로 서비스에 붙여야 하는데 overlay 렌더러는 아직 만들지 않는다.
- local(k3d): Traefik ipAllowList 미들웨어를 써도 Traefik 이 보는 클라이언트 IP 가 SNAT 된 10.42.0.0 이라 전부 막히거나(403) 전부 열린다. 실측으로 확인해 기각한 방법이다.

## 고치는 법
셋 중 하나를 사람이 고른다. 자동으로 고치지 않는다 — allowed_cidrs 를 지우는 것은 보호를 푸는 결정이다.
1. 공개로 열어도 되면 allowed_cidrs 를 지운다 (public: true 그대로).
2. 대역 제한이 꼭 필요하면 aws 로 배포하거나, gcp 는 Cloud Armor 정책을 인프라 쪽에서 먼저 만든다.
3. 클러스터 안에서만 쓰면 network.ingress 자체를 지운다 (Service 만 남고 밖으로 노출하지 않는다).

## 확인
`scripts/render_overlay.py <명세>` 의 종료 코드가 3 이 아니고 경고 목록에 INGRESS_CIDRS_NOT_ENFORCED 가 없다.
