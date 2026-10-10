---
doc_type: guide
provider: local
title: 온프레미스 노출 경로 — 터널 catch-all 과 NodePort 가 내부 전용을 깬다
related_rules: [NET-003]
---
## 터널 ingress
Cloudflare Tunnel 같은 아웃바운드 터널은 라우터 포트를 열지 않고도 공개 주소를 만든다. ingress 규칙에 hostname 없이
`service: http://traefik...` 한 줄(catch-all)만 두면, DNS 레코드가 그 터널을 가리키는 **모든 호스트**가 공개된다.
내부 전용으로 만든 앱도 누군가 DNS 를 터널로 돌리는 순간 인터넷에 열린다.
- 공개할 hostname 만 명시하고 마지막 규칙은 `http_status:404` 로 둔다.
- 공개 범위는 이 파일이 정본이다. 공개 앱을 추가·제거할 때 이 목록을 같이 바꾼다.
- 확인은 내부망 밖에서 curl 로: 공개 대상만 200, 나머지 host 는 404 여야 한다.

## NodePort
NodePort(30000-32767)는 kube-proxy 가 nat PREROUTING 에서 DNAT 하므로 패킷이 호스트 INPUT 체인을 거치지 않는다.
UFW·firewalld 로 INPUT 을 막아도 NodePort 는 열려 있고, `ss -tlnp` 에도 리스닝 소켓이 보이지 않아 놓치기 쉽다.
- 앱 노출은 Ingress(+ 내부 전용이면 내부 진입점)로 하고 NodePort 는 쓰지 않는다.
- 막아야 하면 PREROUTING 단계(mangle 테이블, conntrack NEW)에서 막고 예외는 따로 관리한다.
- 막았는지는 다른 호스트에서 실제로 접속해 본다. 규칙이 있다는 것만으로는 판정할 수 없다.

## 라우터 포트포워딩
80·443 을 노드로 포워딩하면 Traefik 뒤의 모든 Ingress 가 public 값과 무관하게 공개된다.
내부 전용 앱이 있으면 공개용·내부용 진입점을 나누거나 host 기준으로 앞단 프록시에서 거른다.
