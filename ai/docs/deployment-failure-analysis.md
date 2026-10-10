# 배포 실패 분석과 다음 요청의 사례 검토

기존 `POST /webhooks/argocd`에서 Argo CD 오류를 받아 저장하고, 별도 Kafka 작업으로 원인 후보를 분석한다. 검토 상태(`committed` 등)는 유지한다. 실제 배포 결과와 분석 상태는 별도로 조회한다. 배포 후 PR 생성·자동 수정·재배포는 실행하지 않는다.

## 흐름

```text
Argo CD → POST /webhooks/argocd → 이벤트 + 분석 작업 + 초기 실패 사례 (동일 DB 트랜잭션)
                                      ↓ outbox
                            deployment.analysis.requested
                                      ↓
                            전용 워커 → 증거 기반 LLM 분석 → 사례 진단 갱신
                                      ↓
                            /ui: 오류·원인 후보·권장 확인/수정 항목

다음 review.requested → 기존 정적 검사/판단 → 독립된 사례 검색·비교 → 기존 검토·CI 흐름
                                 ↑ 자동 수정 회차마다 현재 명세를 다시 비교
```

사례 조언은 정적 검사 finding이 없어도 실행한다. 기존 verdict를 바꾸거나 과거 조언만으로 자동 커밋하지 않는다. 정적 규칙 승격은 사람이 실패 조건·규칙·회귀 테스트를 별도로 검토한 후 진행한다.

## 송신 계약

- URL·인증 유지: `POST /webhooks/argocd`, `Authorization: Bearer <ARGOCD_WEBHOOK_TOKEN>`.
- 새 계약: [JSON Schema](../../api/schema/deployment.result.v1.schema.json). 필수 `schema_version=deployment.result/v1`, `event_type`, `app`, `env`, `health`.
- 이벤트: `deployed`, `health_degraded`, `sync_failed`. health와 operation.phase를 합치지 않는다.
- 기존 `revision`은 `.app.status.sync.revision`; 실패 작업의 revision은 `operation.revision`에 전달한다.
- 선택 메타데이터: `argocd_app`, `cluster_id`, 대상 `namespace`, `health_message`, `sync_status`, `observed_at`, `operation`, `conditions`, `resources`.
- 시각은 시간대를 포함한다. 누락 선택값은 null/생략, 배열은 빈 목록을 허용한다. 오류 문자열과 목록은 JSON 직렬화한다.
- 본문 최대 256KiB. 오류 문자열 최대 8192자, 리소스/conditions/images 목록 최대 100개.
- 202는 DB 수신 완료; 분석 완료가 아니다. 인증 401(미설정 503), 계약 오류 422, 크기 초과 413, 저장 실패 5xx.
- 이전 5필드 형식도 수신한다. 구형 알림에 operation.phase=Failed/Error가 확장되면 Healthy여도 실패로 처리한다.
- deployed로 잘못 분류된 알림도 phase=Failed/Error 또는 health=Degraded이면 실패로 정규화한다. 구형 기록/신규 관측 저장 사이 또는 성공 관측/baseline 저장 사이에 실패해도 재전송으로 남은 저장을 복구한다.

```json
{
  "schema_version": "deployment.result/v1",
  "event_type": "sync_failed",
  "app": "sample-app",
  "env": "aws",
  "health": "Healthy",
  "sync_status": "OutOfSync",
  "images": [],
  "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "namespace": "sample-app",
  "operation": {
    "phase": "Failed",
    "message": "resource apply failed: forbidden",
    "revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "started_at": "2026-10-09T14:00:00+09:00",
    "finished_at": "2026-10-09T14:01:00+09:00"
  }
}
```

알림의 작업 revision을 검토의 `gitops_commit_sha`와 정확히 연결한다. 여러 검토가 일치하거나 연결할 revision이 없으면 실패를 미연결로 보존한다. 실패 시 이전 stable 이미지나 임의의 최신 검토를 사용하지 않는다. 따라서 GitOps의 후속 이미지 커밋 때문에 SHA가 달라진 알림은 증거만 분석하며 명세별 RAG 사례로는 등록하지 않는다. 이 경우 배포 담당자와 실제 샘플의 식별 정보를 확인해야 한다. 실패 이벤트 연결에 필요한 추가 매핑은 후속 개선 지점이다.

## 저장·재처리

`0011_deployment_analysis.sql`은 기존 테이블에 `case_advice`와 kind 허용값을 추가하고 다음 테이블을 만든다.

- `deployment_observations`: 이벤트별 변경하지 않는 마스킹된 증거·명세 snapshot.
- `deployment_analysis_jobs`: 발행 대기·작업 lease·시도 횟수·분석 결과.
- `deployment_failure_cases`: 같은 앱/레포/환경에서 검색할 실패 사례와 수동 확인된 해결 기록.

관측 시각은 중복 키에서 제외한다. 같은 작업의 달라진 증거는 별도 이벤트로 보존한다. 작업별 자동 분석은 최대 3개 증거 버전, 각 작업의 처리 시도는 최대 3회다. 식별 정보가 부족하면 앱/대상/revision/이미지 범위로 보수적으로 묶어 과금을 제한하고 증거는 계속 보존한다. 이후 증거는 `skipped/EVIDENCE_LIMIT`로 표시한다.

API/워커가 10초마다 outbox를 확인한다. 발행 lease는 30초, 처리 lease는 180초다. Kafka 발행 후 소비 전 재전송될 수 있으므로 DB claim과 lease_token으로 처리한다. SDK 재시도는 0, 진단 호출은 전체 100초 제한이다. DB에서 읽거나 저장할 수 없으면 메시지 처리 예외가 전파되어 Kafka offset을 완료 처리하지 않는다. 분석 출력/인용 오류는 실패 상태, 정보 부족·LLM 불가는 `insufficient`로 남긴다. 오류 코드만 기록하고 외부 예외 원문은 노출하지 않는다.

Kafka 메시지에는 `schema_version=deployment.analysis.requested/v1`, `event_id`만 들어간다. 새 토픽 `deployment.analysis.requested`의 생성과 producer/consumer ACL이 필요하다. 자동 토픽 생성이 비활성화된 환경에서는 기존 Kafka 운영 절차로 미리 생성한다.

## 근거와 사례 적용 판정

LLM 입력은 관측 증거, 정확히 연결된 마스킹 명세, 같은 범위의 사례, 사용 가능한 규칙 문서다. 현재 증거 ID와 사례 ID 인용을 검증한다. 오류 문자열은 지시문으로 실행하지 않는다. 입력 증거는 최대 30개/각 2000자로 제한하며 원문 수신 증거는 별도 보호된 조회에서 확인한다.

다음 요청은 같은 앱·레포·환경의 사례를 검증된 해결 사례 우선·최신순으로 최대 20개 검색하고 작업별 중복을 묶어 최대 5개 조언을 표시한다. 현재 명세의 관련 설정을 비교하며 전체 명세 해시나 오류 문자열 유사도만으로 차단하지 않는다.

- `applicable`: 관련 실패 당시 값과 현재 값이 일치. 미검증 후보와 검증된 원인을 구분한다.
- `resolved`: 현재 관련 설정이 해당 사례에서 확인된 수정값과 모두 일치. 같은 조치를 다시 경고하지 않는다. 전체 배포 성공을 보장하지 않는다.
- `unknown`: 관련 설정 누락, 미검증 변경, 외부 IAM/네트워크 상태 등. 해결됐다고 선언하지 않는다.

초기 사례는 관측 사실만 있고 후보/해결은 비어 있다. AI 분석 완료 후 후보를 기록한다. DB가 사례의 정본이며 동적 Qdrant 색인은 이번 구현에 포함하지 않는다. 재시작 후 DB 사례를 그대로 검색한다.

## 조회·해결 확인

새 상세 API는 `REVIEW_API_TOKEN`으로 인증한 운영자 전용이다. 현재 서비스의 공유 토큰은 모든 앱에 대한 운영자 권한이며 사용자별 권한 체계를 대신하지 않는다. 사용자별 제공 전에는 별도 사용자 인증·대상 권한 체계가 필요하다. 공개 `/reviews/{id}`에는 실제 배포 상태/분석 상태/사건 ID만 추가하고 오류 증거나 사례 내용은 추가하지 않는다.

- `GET /deployments?review_id=...&limit=50` (최대 100): 사건과 분석 결과.
- `GET /deployments/{event_id}`: 연결 명세, 마스킹된 오류, 분석 결과.
- `GET /reviews/{review_id}/case-advice`: 현재 명세를 기준으로 한 사례 조언.
- `POST /failure-cases/{case_id}/resolution`: 운영자가 해당 장애와 관련된 새 수정 배포를 확인한 후 해결 기록.

```json
{
  "success_event_id": "de_<64자리 사건 해시>",
  "cause": "readiness 경로 불일치를 확인했습니다.",
  "actions": ["readiness 경로를 실제 응답 경로로 수정했습니다."],
  "config_paths": ["/runtime/health/readiness"]
}
```

성공 증거는 동일 앱/레포/환경/배포 대상, 실패 후의 다른 검토, Healthy+Synced+Succeeded여야 한다. 실패가 관측된 검토는 해결 근거로 사용할 수 없다. 설정 경로는 두 명세에서 실제 변경된 비밀 아닌 scalar 값이어야 한다. 서버가 당시 값/수정값을 추출한다. 관련 변경과 인과관계는 운영자가 확인해야 하며 임의 Healthy 알림으로 자동 승격하지 않는다. 외부 상태 해결은 config_paths를 비우고 기록할 수 있지만 설정에 의한 경고 억제 조건으로 사용하지 않는다. 토큰 권한의 확인 주체와 시각을 기록한다.

## 연결 순서·검증

1. 혜연님 검토: DB 마이그레이션, 기존 API/worker 메시지 호환성, 새 토픽/ACL 확인.
2. 수신 API·워커 적용 후 성진님과 실제 알림 샘플 확인.
3. Argo CD 신규 템플릿·동기화 실패 트리거 활성화.
4. 정상, Degraded, sync_failed+Healthy, 빈 images, 미연결, 중복 알림으로 UI/분석 확인.

운영 롤백은 새 송신 트리거를 먼저 되돌리고 구형 수신 코드로 전환한다. DB 추가 테이블/열은 유지하여 증거를 보존한다. 신규 필드가 없는 구형 Healthy를 먼저 보내면 동기화 실패와 구분할 수 없으므로 전환 순서를 지킨다.

Pod 종료 원인·이벤트·로그 수집, 플랫폼별 수집기, 성능 메트릭, 자동 복구 PR·재배포는 별도 후속 범위다. 본 기능은 정형 계약을 확장할 수 있으나 Kubernetes 읽기 권한을 요구하지 않는다.

## main #50 통합 검토와 로컬 검증 (2026-10-09)

기준 커밋은 `a5eeb67`이며 작업 브랜치는 `feat/deployment-failure-main50`이다. 앞선 구현의 마이그레이션 번호 0007을 0011로 변경했다. 0010은 review-service#51(`0010_intake_attempts.sql`)이 쓴다. main의 복구 처리, 메트릭, 진행 화면과 단계 시각을 보존했다. 운영 마이그레이션·배포·알림 활성화는 실행하지 않았다.

- API 전체 209개, Worker 전체 122개, AI 전체 893개 통과. AI 커버리지 97.50%.
- 샘플·규칙 검사: 29개 규칙, 10개 샘플, 잘못된 명세 5개. 가짜 LLM 평가 53개 모두 일치.
- 전체 통합 테스트 실행: PostgreSQL 관련 18개 통과, Kafka 관련 4개 실패 (`KafkaConnectionError`, 로컬 19092 연결 닫힘). 추가 환경 격리·동시 중복 알림 테스트를 포함한 PostgreSQL 재검증은 19개 통과, Kafka 4개 제외.
- 실제 PostgreSQL 16에서 0011 적용·멱등성, 동시 수신, 분석 lease, 기존 복구·메트릭 경로 확인. Kafka 통과로 보고하지 않는다. Docker 클라이언트도 10초 내 응답하지 않았다.
- 브라우저에서 #50 진행 화면의 `SyncFailed`, 다른 환경 정상 알림에 가려지지 않는 대상 실패, 운영자 토큰으로 분석 상세 연결을 확인했다. 두 UI JavaScript 구문과 git diff 공백 검사 통과.
- 앞선 기준에서 Kafka 통합 11개가 통과한 이력은 새 main 통합 검증을 대신하지 않는다. 실제 LLM·새 Argo 알림·운영 배포는 검증 전이다. Kafka 재검증 전 병합/알림 전환을 보류한다.

### 환경 격리와 기존 화면

구형 cross_env 알림은 main처럼 `deploy_events`에 기록하며, 다른 환경의 baseline·실패 사례에는 사용하지 않는다. v1 cross_env 오류는 명세 없이 증거 분석 대상으로 저장하고 진행 화면에는 원래 검토의 해당 환경 카드로 투영한다. `deployment_event_id` 유니크 열로 동시 중복 투영을 방지한다. v1 Healthy는 Healthy+Synced+Succeeded를 확인하고 기존 이미지 태그 기준 매칭을 유지한다. 그래야 작업 없이 sync.revision만 바뀌거나 CI 이미지 커밋이 달라도 기존 Healthy 경로가 유지된다.

실패 판정과 모든 baseline SQL은 검토의 대상 환경으로 제한한다. 실패 후 정상 알림만 왔다고 동일 검토의 실패를 지우지 않는다. 해결 사례는 새로운 수정 검토와 운영자의 원인 확인이 필요하다.

### 인프라 담당 작업 대조

[GitOps #38](https://github.com/Crystal-SBHackathon2026/gitops/pull/38)의 `99b8b5f` 기준 템플릿 3개를 검토했다. 템플릿 정합성 검사 통과, 실제 `samples/deployed.json`을 수신 API 회귀 데이터로 고정했다. 이 샘플은 `revision != operation.revision`이며 Healthy baseline 갱신·재전송 시 database_has_data 보존을 확인했다. 실패 샘플은 합성 데이터로만 검증했고 실제 실패 주입 샘플은 아직 없다.

순서: 0011 적용/API·worker 배포 → Kafka 새 토픽/ACL 확인 → 성진님이 템플릿 send 전환·on-sync-failed 구독 적용 → 실제 세 종류 알림 확인. GitOps PR 병합만으로 Notifications ConfigMap/Application이 실제 적용됐다고 판단하지 말고 담당자가 live 적용·구독을 확인해야 한다.

담당 경계는 성진님(template/trigger/Application 구독)과 찬건님(API Gateway/VPC 연결/토큰 공급), 본 작업(API·DB·Kafka·worker 분석)이다. 혜연님은 main #49/#50 호환성·Healthy 회귀 검토를 맡는다. 공유 토큰은 기존 운영자 범위이며 사용자별 인증은 후속이다.

팀 Slack 최신 확인 사항:
- GKE Rollouts·CRD 준비 완료 보고와 별개로 GCP의 Prometheus DNS 실패가 보고됐다. count=6·consecutiveErrorLimit=6으로 오류를 우회할 수 있었다고 가정하면 안 된다. 성진님·찬건님이 AWS 메트릭을 유지하면서 GCP/local 전용 AnalysisTemplate 또는 모니터링 구성을 결정해야 한다. 본 수신 코드의 오류 증거 수집과 별도 문제다.
- AWS FAIL_RATE=0.3 주입은 카나리 중단·stable 200 응답이 실측됐다. 부분 배포 실패와 전체 서비스 장애를 구분한다.
- FAIL_RATE 정적 규칙 후보는 후속 규칙 검토 대상으로 남긴다. 이 변수 의미는 앱 구현에 달려 있으므로 모든 앱에 같은 이름만으로 실패 규칙을 적용하지 않는다. sample-app 한정 조건·고의 장애 실험 처리·회귀 테스트를 함께 검토해야 한다.

로컬 UI 검증 서버는 종료한다. Docker 응답 복구 후 이번 작업의 테스트 프로젝트만 정리해야 한다: `docker compose -p review-failure-it-20261009 -f /private/tmp/review-failure-it-20261009/compose.yml down`. 다른 컨테이너 정리나 Docker 전체 재시작은 실행하지 않았다.
