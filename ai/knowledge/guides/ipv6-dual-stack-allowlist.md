---
doc_type: guide
provider: any
title: IPv4 만 적은 허용·차단 목록은 듀얼스택에서 새거나 끊긴다
related_rules: [NET-002, NET-003]
---
## 왜 문제인가
IPv4 와 IPv6 는 같은 호스트라도 별개 주소다. 허용·차단 목록이 한쪽 계열만 다루면 다른 계열로 들어오는 트래픽은 규칙 밖이다.
- **차단 목록·방화벽**이 IPv4 만 막으면(iptables 만 쓰고 ip6tables 를 비움 등) `[::]` 에서 리스닝하는 서비스가 IPv6 로 그대로 열린다.
- **허용 목록**이 IPv4 만 허용하면 IPv6 로 오는 정상 클라이언트가 조용히 드롭된다. 브라우저는 Happy Eyeballs 로 IPv4 에 폴백해
  증상이 안 보이다가, IPv6 만 있는 망(셀룰러·IPv6 전용 VM)에서만 장애가 난다.
- split DNS·hosts 파일에 A 레코드만 넣으면 IPv6 우선 클라이언트가 AAAA 를 공개 DNS 에서 찾아 내부 대신 공개 경로로 나간다.

## 이 명세로 판정하지 않는 이유
network.ingress.allowed_cidrs 에 IPv4 만 있어도 지금 대상 환경에서는 문제가 되지 않거나 판정할 수 없다.
- aws: overlay 가 ip-address-type 을 지정하지 않아 ALB 가 IPv4 전용이다. IPv6 클라이언트는 애초에 닿지 않는다.
- gcp·local: allowed_cidrs 자체를 overlay 가 강제하지 못해 INGRESS_CIDRS_NOT_ENFORCED 가 커밋을 막는다.
- 진입점이 듀얼스택인지(대상 환경의 IP 계열)는 명세에도 catalog/targets.yaml 에도 없다.
대상 환경에 듀얼스택 진입점이 생기면 그때 targets.yaml 에 IP 계열을 적고 규칙으로 올린다.

## 확인하는 법
- 방화벽·ACL·허용 목록을 바꾸면 IPv4·IPv6 양쪽으로 실제 접속해 본다. 집·사무실 망에 IPv6 가 없으면 셀룰러 핫스팟에서.
- 서버에서 `ss -tlnp` 로 `[::]:<포트>` 리스닝 여부를 보고, IPv6 방화벽 규칙이 같은 정책인지 확인한다.
