---
warning_code: TLS_HOST_MISSING
doc_type: warning
provider: any
blocking: true
title: TLS 를 요구했는데 host 가 없어 인증서를 붙일 수 없다
---
## 왜 멈추나
network.ingress.tls 가 true 인데 host 가 비어 있다. 인증서는 도메인 이름에 붙으므로 host 없이는 TLS 설정을 만들 수 없고,
그대로 커밋하면 Ingress 가 **평문 HTTP 로** 열린다. 명세가 요구한 암호화가 빠지는 것이라 중단한다.

## 고치는 법
- 도메인이 있으면 network.ingress.host 를 채운다. 인증서 준비는 TLS_SECRET_REQUIRED(local·gcp)·ACM(aws) 문서를 따른다.
- 아직 도메인이 없으면 tls 를 false 로 바꾼다. 공개 진입점이면 NET-001 경고(low)가 남고 배포는 진행된다.
자동으로 고치지 않는다 — 어느 쪽이 맞는지는 도메인 준비 여부에 달렸다.

## 확인
렌더 결과 Ingress 에 host 가 있고 (local·gcp 는) `tls[0].hosts` 에 같은 host 가 들어 있다.
