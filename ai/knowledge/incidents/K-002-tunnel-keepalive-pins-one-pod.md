---
doc_type: incident
provider: any
title: 터널 뒤에서 공개 URL 로 여러 번 요청해도 복제 Pod 문제를 잡지 못한다
card_id: K-002
related_rules:
- DB-003
observed: 2026-10-01 / pc / 검증
---
## 증상
replicas 2 + 메모리 세션 + Pod 별 SQLite 인 trap 앱이 공개 URL 읽기 6회를 모두 통과했다.

## 원인
cloudflared 가 Service 로 keep-alive 연결을 재사용하고 kube-proxy 는 연결 단위로 Pod 를 고른다. 순차 요청은 전부 같은 Pod 로 간다. 실제 사용자 트래픽에서는 연결이 늘면서 뒤늦게 드러난다.

## 고치는 법
공개 URL 로 로그인·쓰기 후 같은 세션 쿠키로 각 Pod 에 직접(port-forward) 읽는 replica-consistency 검사. port-forward 는 Pod loopback 이라 NetworkPolicy 를 바꾸지 않는다.

## 확인
trap 에서 "2개 Pod 중 1개 HTTP 401" 로 실패, fixed-manual 에서 통과.
