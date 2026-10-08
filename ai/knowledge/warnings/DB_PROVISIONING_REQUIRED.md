---
warning_code: DB_PROVISIONING_REQUIRED
doc_type: warning
provider: any
blocking: true
title: overlay 로 만들지 않는 DB(managed·in-cluster·external)라 인프라 프로비저닝이 먼저다
---
## 왜 멈추나
database.placement 가 volume(SQLite) 이 아니면 DB 는 overlay 밖에서 만들어져 있어야 한다. 검토는 명세가 맞는지만 보고 통과시키지만,
없는 DB 를 바라보는 앱은 뜨지 않으므로 커밋 단계에서 멈춘다.

## 환경별
- aws: RDS postgres 16 은 있지만 앱마다 DB 를 만드는 수단(Crossplane 등)이 아직 없다. in-cluster·volume 은 EBS CSI 가 없어 불가.
- gcp: Cloud SQL 은 Terraform 미작성. in-cluster postgres 16 과 volume sqlite 는 능력표상 가능하다.
- local: in-cluster postgres 16, volume sqlite 가 가능하다.

## 고치는 법
1. 인프라 쪽(Terraform)에서 DB 와 접속 정보 시크릿(SEC-005)을 먼저 만들고 다시 배포한다.
2. 데이터가 없고 단일 replica 로 충분하면 placement 를 volume + engine sqlite 로 바꾸는 것도 방법이다 (sqlite-vs-postgres 가이드).
   엔진·배치 변경은 사람이 고른다 — 데이터가 있으면 DB-001 로 막힌다.

## 확인
DB 엔드포인트가 접속 정보 시크릿에 들어 있고, 앱 Pod 가 readiness 를 통과한다.
