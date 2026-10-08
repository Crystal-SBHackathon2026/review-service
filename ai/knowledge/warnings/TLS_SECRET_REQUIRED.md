---
warning_code: TLS_SECRET_REQUIRED
doc_type: warning
provider: any
blocking: false
title: TLS 인증서 Secret(<앱>-tls)이 미리 있어야 한다
---
## 무슨 뜻인가
local·gcp 에서 tls: true 이고 host 가 있으면 Ingress 가 `<앱>-tls` Secret 의 인증서를 쓴다. overlay 는 이 Secret 을 만들지 않는다.
없어도 배포는 멈추지 않는다 — Ingress 컨트롤러가 자기 기본(자체 서명) 인증서로 응답해 브라우저 경고가 뜰 뿐 평문 노출은 없다.
aws 는 ALB 가 ACM 인증서를 쓰므로 이 경고가 나오지 않는다.

## 고치는 법
cert-manager Certificate 로 `<앱>-tls` 를 발급하거나, 가진 인증서로 `kubectl create secret tls <앱>-tls` 를 앱 namespace 에 만든다.

## 확인
`curl -v https://<host>` 의 인증서 subject 가 host 와 맞다.
