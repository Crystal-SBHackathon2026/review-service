---
doc_type: incident
provider: any
title: NetworkPolicy 가 있는 k3s 에서 새 Pod 의 첫 DB 연결이 거부된다
card_id: K-001
related_rules: []
observed: 2026-10-01 / pc (k3d, k3s v1.31 kube-router) / 롤아웃
---
## 증상
DB 는 이미 준비돼 있는데 새 앱 Pod 가 시작 직후 ECONNREFUSED 로 죽고 한 번 재시작된다. RESTARTS 1 이 매 배포마다 남는다.

## 원인
네트워크 정책 컨트롤러가 새 Pod IP 를 허용 집합에 넣기 전에 앱이 첫 연결을 보낸다. Postgres 로그상 준비 완료(13:17:03)와 앱 실패(13:17:25)의 시각이 이를 뒷받침한다.

## 고치는 법
앱 시작 시 DB 연결을 지수 백오프로 재시도한다 (프로세스를 바로 종료하지 않는다).

## 확인
재배포 후 앱 Pod RESTARTS 0, 로그에 "DB 연결 재시도 1/10" 뒤 정상 기동.
