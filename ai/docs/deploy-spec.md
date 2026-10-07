# deploy_spec 초안 (crystal.review/v1alpha1)

검토 서비스가 받는 **배포 요청 명세**다. static_check, 근거 검색, 평가셋이 모두 이 형식을 입력으로 쓴다.
위험 목록 5번("deploy_spec 형식을 맡은 사람이 없다")을 메우려는 제안이며, 회의에서 확정한다.

## 파일

| 파일 | 내용 |
|---|---|
| `review_ai/spec/deploy_spec.py` | Pydantic 모델(정본). 형식 검사와 `load_spec()` |
| `schema/deploy_spec.schema.json` | 위 모델에서 내보낸 JSON Schema. 파이프라인 쪽 FastAPI 나 다른 언어가 쓸 수 있다 |
| `catalog/rules.yaml` | 정적 검사 규칙 26개 (P0 11개) |
| `catalog/targets.yaml` | 대상 환경 능력표 (aws·gcp·local). 아직 실측 전 |
| `samples/*.yaml` | 샘플 명세 10개 |
| `samples/cases.yaml` | 샘플별 기대 finding·verdict·사유 |
| `scripts/validate_samples.py` | 위 파일이 서로 맞는지 확인하고 스키마를 다시 내보낸다 |

## 형식 오류와 finding 의 경계

- **형식 오류**(필드 누락, 타입, 이름 규칙)는 Review API 가 **422 로 거절**한다. 검토 그래프에 들어가지 않는다.
- **의미 문제**(SQLite 복제, 평문 시크릿, 엔진 변경 등)는 형식상 통과시키고 static_check 가 **finding** 으로 낸다.
  모델에서 막으면 사용자는 "왜 안 되는지" 설명과 수정안을 받지 못하기 때문이다.

## 구조

```yaml
api_version: crystal.review/v1alpha1
kind: DeploySpec
metadata: {name, repository, commit}          # 어떤 앱의 어떤 커밋인가
target:   {env: aws|gcp|local, region, namespace?}
image:    {repository, tag?, digest?, platforms: [amd64|arm64]}
runtime:  {port, replicas, health{readiness,liveness}, resources, env{평문만}, termination_grace_seconds}
requirements: {persistence}                    # 재배포 뒤에도 데이터가 남아야 하는가
database: {engine: none|postgres|mysql|sqlite, version, placement: managed|in-cluster|volume|external,
           engine_policy: preserve|allow_convert, volume, env_var, backup_retention_days, publicly_accessible}
secrets:  [{name, source: aws-secrets-manager|gcp-secret-manager|k8s-secret|generated, key}]   # 값은 넣지 않는다
network:  {ingress: {public, tls, host, allowed_cidrs} | null}
storage:  {volumes: [{name, mount_path, size, persistent, access_mode}], buckets: [{name, public, versioning, encryption}]}
baseline: {spec_ref, spec: <이전 명세>, facts: {database_has_data, observed_at}} | null   # 파이프라인이 채움
```

필드 이름은 State 와 맞춰 snake_case 로 했다. 카테고리(database·secret·network·storage)가 그대로 블록이라
규칙이 어느 블록을 보는지 바로 보인다.

### baseline — 사용자가 아니라 파이프라인이 채운다

엔진 변경(DB-001), 볼륨 축소(STO-005)처럼 **이전 상태와 비교해야 하는 규칙**이 있다.
파이프라인이 업무 DB 에서 같은 앱·같은 환경의 마지막 승인 배포 명세를 찾아 `baseline.spec` 에 넣고,
데이터 유무 같은 관측 사실을 `baseline.facts` 에 넣는다. 첫 배포면 `null`.

- `database_has_data` 를 모르면(`null`) 규칙은 **데이터가 있다고 가정**한다. 모르는 상태로 자동 변경하지 않기 위해서다.
- 이 덕분에 위험 1번(데모의 DB 엔진 자동 변경)을 규칙 하나로 정리할 수 있다.
  첫 배포·데이터 없음이면 DB-002 가 `allowed`(샘플 05), 데이터가 있으면 DB-001 이 `forbidden`(샘플 04)이다.

### 비밀 값은 명세에 넣지 않는다

`secrets` 에는 어디서 읽을지만 적는다. `runtime.env` 에 비밀처럼 보이는 평문이 있으면 SEC-001 이 잡고,
evidence 에도 값을 남기지 않는다. `source: generated` 는 배포 시 무작위 값을 만들어 k8s Secret 으로 넣는다는 뜻이다(생성 주체는 미정).

## 규칙 요약

| 카테고리 | P0 (먼저 구현) | P1 |
|---|---|---|
| database | DB-001 데이터 있는 엔진 변경 · DB-002 지원 안 되는 엔진·배치 · DB-003 SQLite 복제 · DB-005 SQLite 영속 볼륨 없음 | DB-004 · DB-006 · DB-007 · DB-008 |
| secret | SEC-001 평문 비밀 · SEC-005 DB 접속 시크릿 없음 | SEC-002 · SEC-003 · SEC-004 |
| network | NET-001 TLS 없음 (low) | NET-002 |
| storage | STO-003 공개 버킷 · STO-005 볼륨 축소 | STO-001 · STO-002 · STO-004 · STO-006 |
| runtime | RUN-001 readiness 없음 · RUN-004 아키텍처 불일치 | RUN-002 · RUN-003 · RUN-005 |

`autofix` 는 규칙에 `allowed` / `forbidden` / `when_no_data` 로 적고, Finding 을 만들 때 인스턴스마다 `allowed` 나 `forbidden` 으로 확정한다.
되돌릴 수 없는 변경(DB-001·DB-008·STO-005·STO-006)은 `irreversible: true` 로 표시해 사유 코드를 IRREVERSIBLE 로 낸다.

## 샘플

| # | 파일 | 기대 finding | verdict |
|---|---|---|---|
| 01 | sample-app 그대로 (aws) | NET-001 (low) | pass |
| 02 | SQLite 단일 replica, 로컬 | 없음 → LLM 호출 안 함 | pass |
| 03 | SQLite + replicas 2, gcp 첫 배포 | DB-003 | fix → pass |
| 04 | 데이터 있는 postgres → mysql | DB-001 | needs_human (IRREVERSIBLE) |
| 05 | 로컬에 mysql 요청, 첫 배포 | DB-002 | fix → pass |
| 06 | 평문 API 토큰 | SEC-001 | needs_human |
| 07 | 공개 버킷 | STO-003 | fix → pass |
| 08 | 볼륨 10Gi → 5Gi | STO-005 | needs_human (IRREVERSIBLE) |
| 09 | arm64 전용 이미지 → amd64 노드 | RUN-004 | needs_human |
| 10 | 공개 버킷 + readiness 없음 | RUN-001, STO-003 | needs_human |

01 은 실제 sample-app 과 gitops aws overlay 값을 그대로 옮겼다. 나머지 앱 이름(todo·orders·gallery)과 digest 는 샘플 값이다.
검증 결과(2026-10-07): 규칙 26개, 샘플 10개 형식 통과, 형식 오류 예시 5개 거절.
**기대 finding 은 손으로 도출한 값**이라 static_check 를 구현하면서 이 표로 테스트해 맞춘다.

## 회의에서 정할 것

1. **명세를 누가 어디에 쓰나.** 앱 레포의 `deploy.yaml` 을 커밋하고 Review API 는 `spec_ref`(레포·커밋·경로)로 받는가, 요청 본문으로 받는가.
2. **baseline 위치.** `deploy_spec.baseline` (이 초안) 과 `State.baseline_spec` 별도 필드 중 하나. 채우는 쪽은 파이프라인.
3. **Finding.category 에 `runtime` 추가.** readiness·아키텍처는 Rollout 자동 중단과 직결돼 빼기 어렵다.
4. **decide_verdict 보완.** low finding 은 `forbidden` 이어도 사람 조건에서 빼야 한다.
   안 그러면 HTTP 전용인 지금의 sample-app(NET-001)이 매번 needs_human 이 된다.
5. **명세 → kustomize overlay 변환은 누가 하나.** 지금 gitops 는 overlay 를 손으로 쓴다. 검토 서비스의 patch 는 명세를 고치는데, 그게 overlay 에 반영돼야 한다.
6. **능력표 실측.** EKS 노드 아키텍처, GKE·Cloud SQL 유무, busan-local 노드 아키텍처.
7. **`source: generated` 시크릿은 누가 만드나.**

## 범위 밖

SIGTERM 처리, Dockerfile root 실행처럼 **코드·이미지를 봐야 하는 검사**는 이 명세로 판단할 수 없다.
oneaction 의 코드 탐지 규칙(R-SIGTERM-MISSING 등)은 CI 단계에 별도로 두고, 명세 검사와 섞지 않는다.
