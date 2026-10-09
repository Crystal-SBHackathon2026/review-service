# deploy_spec 초안 (crystal.review/v1alpha1)

검토 서비스가 받는 **배포 요청 명세**다. static_check, 근거 검색, 평가셋이 모두 이 형식을 입력으로 쓴다.
위험 목록 5번("deploy_spec 형식을 맡은 사람이 없다")을 메우려는 제안이며, 회의에서 확정한다.

## 파일

| 파일 | 내용 |
|---|---|
| `review_ai/spec/deploy_spec.py` | Pydantic 모델(정본). 형식 검사와 `load_spec()` |
| `schema/deploy_spec.schema.json` | 위 모델에서 내보낸 JSON Schema. 파이프라인 쪽 FastAPI 나 다른 언어가 쓸 수 있다 |
| `catalog/rules.yaml` | 정적 검사 규칙 27개 (P0 12 + P1 15, 전부 구현 — RUN-003 은 10/09 폐기) |
| `catalog/targets.yaml` | 대상 환경 능력표 (aws·gcp·local). aws 만 실측(10/08) |
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
metadata: {name, repository, commit?}         # commit 은 앱 레포 deploy.yaml 이면 비운다 — 검토한 커밋은 spec_ref.commit
target:   {env: aws|gcp|local, region, namespace?}
image:    {repository, tag?, digest?, platforms: [amd64|arm64]}   # tag·digest 는 참고용 — 배포 태그는 CI 가 gitops base 에 쓴다
runtime:  {port, replicas, health{readiness,liveness}, resources, env{평문만}, termination_grace_seconds}
requirements: {persistence}                    # 재배포 뒤에도 데이터가 남아야 하는가
database: {engine: none|postgres|mysql|sqlite, version, placement: managed|in-cluster|volume|external,
           engine_policy: preserve|allow_convert, volume, env_var, backup_retention_days, publicly_accessible,
           migration: {command: [..], change: none|expand|contract|breaking} | null}   # postgres·mysql 만
secrets:  [{name, source: aws-secrets-manager|gcp-secret-manager|k8s-secret|generated, key}]   # 값은 넣지 않는다
network:  {ingress: {public, tls, host, allowed_cidrs} | null}
storage:  {volumes: [{name, mount_path, size, persistent, access_mode}], buckets: [{name, public, versioning, encryption}]}
rollout:  {strategy: canary|bluegreen}         # 생략하면 canary
smoke:    {paths: [/healthz, ..]} | null        # 배포 뒤 클러스터 안에서 GET 할 경로 (최대 10)
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

### 배포 계획 — 마이그레이션 시점과 전략 (`review_ai/deploy_plan.py`)

배포 중에는 옛 버전과 새 버전이 잠깐 같이 돈다. 그 구간에 두 버전이 같은 스키마·같은 DB 에서 돌 수 있는지로 정한다.

| 변경 | 마이그레이션 Job (Argo CD hook) | 전략 |
|---|---|---|
| `none`·`expand` (추가만) | PreSync — 새 버전보다 먼저. 실패하면 동기화가 멈춰 새 버전이 뜨지 않는다 | canary |
| `contract` (제거만) | PostSync — 새 버전이 다 뜬 뒤 | canary |
| `breaking` (이름·타입 변경) | PreSync | bluegreen + 사람 승인 (RUN-006) |
| 이전 배포와 DB 엔진·배치가 다름 | — | bluegreen (RUN-007) |

- 렌더러는 bluegreen 이면 Rollout `strategy` 를 통째로 `blueGreen` 으로 바꾸고 `<앱>-preview` Service 를 만든다. canary 는 base 를 그대로 쓴다.
- 마이그레이션 Job 은 앱과 같은 이미지로 돈다 — kustomize `replacements` 가 Rollout 컨테이너 이미지(CI 가 태그를 쓴 값)를 복사한다.
  재시도 없음(`backoffLimit: 0`), 5분 제한, env·시크릿은 앱 컨테이너와 같다.
- 생성 명세(prepare_spec)도 같은 함수(`choose_strategy`)로 전략을 고른다.
- `smoke` 가 있으면 PostSync Job(`curlimages/curl`)이 Rollout 이 Healthy 가 된 뒤 `http://<앱>:80<경로>` 를 GET 한다.
  경로마다 5번까지 다시 시도하고, 하나라도 실패하면 동기화가 실패로 끝나 Argo CD 알림(on-sync-failed)으로 이어진다.
  레포 분석은 **테스트 파일이 하나도 없을 때만** readiness 경로 + 소스의 헬스·정보성 고정 GET 라우트로 이 값을 채운다.

### 비밀 값은 명세에 넣지 않는다

`secrets` 에는 어디서 읽을지만 적는다. `runtime.env` 에 비밀처럼 보이는 평문이 있으면 SEC-001 이 잡고,
evidence 에도 값을 남기지 않는다. `source: generated` 는 배포 시 무작위 값을 만들어 k8s Secret 으로 넣는다는 뜻이다(생성 주체는 미정).

## 규칙 요약

| 카테고리 | P0 | P1 |
|---|---|---|
| database | DB-001 데이터 있는 엔진 변경 · DB-002 지원 안 되는 엔진·배치 · DB-003 SQLite 복제 · DB-005 SQLite 영속 볼륨 없음 | DB-004 영속 저장소 없음 · DB-006 관리형 DB 공개 · DB-007 관리형 DB 백업 꺼짐 · DB-008 메이저 버전 다운그레이드 |
| secret | SEC-001 평문 비밀 · SEC-005 DB 접속 시크릿 없음 | SEC-002 대상 환경에 없는 비밀 저장소 · SEC-003 시크릿 이름 중복 · SEC-004 env·secrets 이름 중복 |
| network | NET-001 TLS 없음 (low) | NET-002 내부 전용인데 전체 대역 허용 |
| storage | STO-001 접근 모드 미지원 · STO-003 공개 버킷 · STO-005 볼륨 축소 | STO-002 RWO 볼륨 복제 · STO-004 버킷 암호화 꺼짐 · STO-006 persistent 볼륨 제거 |
| runtime | RUN-001 readiness 없음 · RUN-004 아키텍처 불일치 | RUN-002 liveness 없음 (low) · RUN-005 리소스 상한 없음 (low) · RUN-006 호환 안 되는 스키마 변경 · RUN-007 두 버전이 함께 돌면 안 되는데 canary |

카탈로그의 규칙 27개를 모두 구현했다. RUN-003(이미지 digest 고정 없음)은 폐기했다 — 배포 이미지는 CI 가 gitops base 에
커밋 SHA 태그로 고정하고 명세의 `image.tag`·`digest` 는 렌더러가 쓰지 않는다. 병합 전 PR 에는 digest 가 아직 없어 모든 검토에 고칠 수 없는 경고가 붙는다.

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
5. ~~**명세 → kustomize overlay 변환은 누가 하나.**~~ → 검토 서비스 코드(`review_ai/overlay`)가 만든다. 지금 sample-app overlay 3개를 그대로 재현한다 — [review-ai.md](review-ai.md#overlay-렌더러-결정-7-검토-서비스-코드가-만든다)
6. **능력표 실측.** ~~EKS~~ → aws 실측 반영(10/08): amd64, EBS CSI 없음 → 볼륨·in-cluster DB·SQLite 볼륨 불가, 앱별 DB 생성 수단 없음. 남은 것: GKE·Cloud SQL 유무, busan-local 노드 아키텍처.
7. **`source: generated` 시크릿은 누가 만드나.**

## 범위 밖

SIGTERM 처리, Dockerfile root 실행처럼 **코드·이미지를 봐야 하는 검사**는 이 명세로 판단할 수 없다.
oneaction 의 코드 탐지 규칙(R-SIGTERM-MISSING 등)은 CI 단계에 별도로 두고, 명세 검사와 섞지 않는다.
