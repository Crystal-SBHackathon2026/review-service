---
warning_code: SECRET_KEYS_REQUIRED
doc_type: warning
provider: any
blocking: false
title: 앱 Secret(<앱>-secrets)에 명세의 키가 미리 있어야 한다
---
## 무슨 뜻인가
overlay 는 시크릿을 `secretKeyRef`(`<앱>-secrets`)로만 연결하고 값은 넣지 않는다. 명세 secrets 에 적힌 키가 그 Secret 에 없으면
새 Pod 가 CreateContainerConfigError 로 시작하지 못한다. 그래도 배포를 막지는 않는다 — Rollout 이 60초 안에 준비되지 않으면
스스로 중단하고 기존(stable) 버전을 계속 서비스하기 때문이다 (rollout-health-gate 가이드).

## 환경별
- aws: ESO 동기화는 아직 platform namespace 에만 있다. 앱 namespace 의 aws-secrets-manager 시크릿은 직접 만들어야 한다.
- gcp·local: k8s Secret 을 직접 만든다. 값 생성·동기화 주체는 결정 #8 에서 정한다.

## 고치는 법
배포 전에 `kubectl -n <ns> create secret generic <앱>-secrets --from-literal=<키>=...` 처럼 키를 모두 채운다.
값을 명세·runtime.env 에 평문으로 옮기지 않는다 (SEC-001).

## 확인
`kubectl -n <ns> get secret <앱>-secrets -o jsonpath='{.data}'` 에 명세의 키 이름이 모두 있다.
