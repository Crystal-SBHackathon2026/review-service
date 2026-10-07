---
doc_type: incident
provider: any
title: express-session 기본 MemoryStore 와 코드에 박힌 secret — 복제·재시작에서 로그인이 풀리고 쿠키를 위조할
  수 있다
card_id: K-011
related_rules:
- SEC-001
observed: 2026-10-01 / pc / trap 브랜치 --force 배포 (1일차)
---
## 증상
공개 URL 에서 로그인 후 같은 쿠키로 각 Pod 를 직접 읽으면 한 Pod 는 401 이다 (복제 일관성 실패). 재시작 뒤에도 세션이 사라진다. secret 은 'dev-secret' 문자열이 레포에 있다.

## 원인
store 를 지정하지 않으면 express-session 은 프로세스 메모리(MemoryStore)에 세션을 둔다 — Pod 마다 따로이고 재시작하면 없다. 서명 키가 코드에 있으면 레포를 본 누구나 유효한 세션 쿠키를 만들 수 있다.

## 고치는 법
세션을 공유 저장소에 둔다 (Postgres: connect-pg-simple, SQLite 볼륨 + replicas 1: 같은 DB 파일). secret 은 app.yaml secrets 에 generate: true 로 선언해 타깃 안에서 만들고 환경변수 SESSION_SECRET 으로 받는다.

## 확인
trap(--force) replica-consistency 실패 · fixed-manual 복제 2 Pod 직접 읽기 ok (3일차 PC d-0b534611e20e). 공개 URL 순차 요청만으로는 드러나지 않는다 (K-002).
