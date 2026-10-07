---
doc_type: incident
provider: any
title: SQLite 파일이 컨테이너 임시 파일 시스템(/tmp)에 있어 재시작하면 데이터가 사라진다
card_id: K-009
related_rules:
- DB-005
- DB-003
observed: 2026-10-01 / pc / trap 브랜치 --force 배포 (1일차)
---
## 증상
공개 URL 로 가입·쓰기·읽기는 통과하는데, 재시작 후 보존 검사에서 "재시작 후 로그인 401" 로 실패한다. replicas 2 에서는 Pod 마다 다른 DB 파일을 써서 복제 일관성 검사도 실패한다.

## 원인
new Database('/tmp/todo.db') — 컨테이너 쓰기 계층은 Pod 가 바뀌면 버려진다. 볼륨을 선언하지 않았으므로 타깃이 어디든(PC·VM) 같은 결과다. 엔진 문제가 아니라 파일 위치 문제다.

## 고치는 법
둘 중 하나. (1) 영속 볼륨을 선언(app.yaml database.volume)하고 경로를 환경변수(DATA_DIR)로 받으며 replicas 1 (good-sqlite). (2) PostgreSQL 로 옮기고 DATABASE_URL 로 받는다 (fixed-manual). 엔진은 사용자가 고른다 — 조용히 바꾸지 않는다.

## 확인
trap(--force) 보존 검사 실패 · good-sqlite(볼륨) 재배포 후 데이터 보존 통과 · fixed-manual(Postgres) 보존 통과 (1일차 PC).
