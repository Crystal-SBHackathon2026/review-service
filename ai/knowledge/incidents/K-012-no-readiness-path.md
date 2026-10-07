---
doc_type: incident
provider: any
title: 준비 상태 경로가 없으면 깨진 릴리스를 롤아웃 단계에서 막지 못하고 외부 기능 검사에만 의존한다
card_id: K-012
related_rules:
- RUN-001
observed: 2026-10-01 / 성적표 / trap 브랜치 분석 (1일차)
---
## 증상
trap 은 app.yaml 에 runtime.health 가 없다. 성적표의 자동 롤백 행이 타깃이 롤백을 지원해도 conditional 로 떨어진다.

## 원인
readiness 가 없으면 k8s 는 프로세스가 뜨자마자 트래픽을 보낸다. DB 연결 전·초기화 중 요청이 실패하고, 롤링 업데이트가 새 Pod 의 준비를 기다리지 않아 깨진 릴리스도 롤아웃은 성공으로 끝난다.

## 고치는 법
/livez(프로세스 생존)와 /readyz(DB SELECT 1 까지)를 두고 app.yaml runtime.health 에 선언한다. readiness 를 통과해도 기능이 깨질 수 있으므로 외부 기능 검사·자동 롤백은 그대로 둔다 (broken-release 가 그 예).

## 확인
fixed-manual·good-sqlite 성적표 자동 롤백 supported(실측 타깃) · trap conditional. broken-release 는 readiness 통과 후 쓰기→읽기 실패로 롤백 — readiness 만으로 충분하지 않다는 반례(3일차).
