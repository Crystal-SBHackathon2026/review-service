# review-service

배포 요청 명세(deploy_spec)를 검토해 `pass` / `fix` / `needs_human` 을 내는 AI 리뷰 서비스.

```
Review API (FastAPI) ─▶ Kafka review.requested ─▶ LangGraph 워커
                                                   static_check → retrieve_evidence → judge → verdict 분기
```

| 담당 | 범위 |
|---|---|
| 파이프라인 | Review API, Kafka, 워커 틀, 업무 DB, 커밋 단계 |
| AI 판단 | 정적 검사 규칙, 문서 색인, 근거 검색, LLM 판단, 평가셋 |

## 디렉터리

| 경로 | 내용 |
|---|---|
| `ai/` | AI 판단 쪽 — 명세 모델, 규칙, 판단 노드, overlay 렌더러, 근거 문서, 평가셋. 연결 안내는 [ai/docs/review-ai.md](ai/docs/review-ai.md) |
| `api/` | Review API (FastAPI, 8080) — 검토 요청, 사람 결정, `/verify`, GitHub·Argo CD 웹훅 |
| `worker/` | 워커 — Kafka `review.requested`·`review.resumed` → LangGraph 검토 그래프 (체크포인트는 업무 DB) |
| `common/` | API·워커 공통 — 업무 DB 저장소·마이그레이션(`review_common/migrations/`), `review.resumed` 메시지, GitHub 클라이언트 |
| `tests/integration/` | 실제 Postgres·Kafka 연동 테스트 |

## 로컬 개발

```bash
uv venv -p 3.13 .venv
uv pip install -p .venv -e "./ai[dev]" -e ./common -e "./api[dev]" -e "./worker[dev]"

# 단위 테스트 (외부 의존 없음)
(cd ai && ../.venv/bin/pytest -q) && (cd api && ../.venv/bin/pytest -q) && (cd worker && ../.venv/bin/pytest -q)

# Kafka·Postgres 띄우고 통합 테스트
docker compose up -d kafka postgres
REVIEW_IT_DSN=postgresql://review:review@localhost:5432/oneaction_review KAFKA_BOOTSTRAP=localhost:9092 \
  .venv/bin/pytest -q tests/integration

# API·워커까지 컨테이너로 (http://localhost:8080)
docker compose --profile app up -d --build
```

## 환경변수

| 이름 | 쓰는 곳 | 설명 |
|---|---|---|
| `DB_HOST` `DB_PORT` `DB_NAME` `DB_USERNAME` `DB_PASSWORD` | API·워커 | 업무 DB. 클러스터에서는 계정을 `review-db-credentials` Secret 에서 |
| `DB_SSLMODE` | API·워커 | 기본 `prefer`. RDS 는 `require` |
| `KAFKA_BOOTSTRAP` | API·워커 | MSK PLAINTEXT bootstrap (Terraform `infra/msk` output `bootstrap_brokers`) |
| `KAFKA_SEND_TIMEOUT` | API·워커 | 발행 한 건을 기다리는 상한(초). 기본 `5` — GitHub 웹훅 10초 안에 503 으로 답한다 |
| `REVIEW_STALE_AFTER` | API | review sweep 이 멈췄다고 볼 시간(초). 기본 `600` — judge 최악(약 8분)보다 길게 |
| `WORKER_ALIVE_FILE` | 워커 | livenessProbe 가 보는 파일. 기본 `/tmp/worker-alive` |
| `METRICS_PORT` | 워커 | Prometheus `/metrics` 포트. 기본 `9100` (API 는 `8080/metrics`) |
| `GITHUB_TOKEN` | API·워커 | API 는 deploy.yaml 읽기와 명세 생성 커밋(PR 브랜치 Contents 쓰기)·PR 커밋 상태(**Commit statuses 쓰기**, 없으면 표시만 빠진다). 워커는 CI 상태 조회·AI 수정 커밋·PR 병합·gitops overlay 커밋이라 앱 레포·gitops Contents·Pull requests **쓰기** 권한이 필요하다 (`oneaction/gitops-token`) |
| `GITOPS_REPO` | 워커·API | overlay 를 커밋할 gitops 레포. 기본 `Crystal-SBHackathon2026/gitops`. API 는 진행 화면의 gitops 커밋 링크에만 쓴다 |
| `GITHUB_WEBHOOK_SECRET` | API | `/webhooks/github` HMAC 검증. 없으면 웹훅을 503 으로 거절 |
| `GITHUB_CI_APP_SLUG` | API·워커 | 이 GitHub App 의 `check_suite` 만 CI 결과로 본다. 기본 `github-actions`, 빈 값이면 전부 |
| `DEFAULT_TARGET` | API | baseline 이 없는 레포에 명세를 만들 대상 `env/region` (예 `aws/ap-northeast-2`). 없으면 그런 레포는 `NO_TARGET` |
| `INTAKE_REPOSITORIES` | API | `owner/repo,…` — baseline 이 없어도 `deploy.yaml` 없음을 intake 로 볼 레포(새 앱). 웹훅이 조직 단위라 목록·baseline 에 없는 레포는 예전처럼 skip |
| `REVIEW_API_PUBLIC_URL` | API·워커 | PR 커밋 상태의 링크 앞부분 — `review-service/verify` 는 진행 화면 `/ui/reviews/{id}`, `review-service/intake` 는 `/intakes/{id}`. 없으면 링크 없이 표시 |
| `DEPLOY_ENVS` | API | 진행 화면의 실제 환경 카드(배포 알림이 오는 환경), 쉼표 구분. 기본 `aws,local` |
| `PLANNED_ENVS` | API | 진행 화면에 "계획"으로만 보이는 환경. 기본 `gcp`, 빈 값이면 없음 |
| `APP_URLS` | API | 진행 화면의 앱 주소 JSON `{"sample-app": {"aws": "http://…", "local": "http://…"}}`. http(s) 만 받는다. 없으면 주소 없이 표시 |
| `ARGOCD_WEBHOOK_TOKEN` | API | `/webhooks/argocd` 의 `Authorization: Bearer <토큰>`. **비어 있으면 503** (fail-closed) |
| `REVIEW_API_TOKEN` | API | `POST /reviews`·`POST /reviews/{id}/decision` 의 `Authorization: Bearer <토큰>`. **비어 있으면 503** (fail-closed) |
| `ANTHROPIC_API_KEY` `REVIEW_LLM_MODEL` | 워커·API | 워커: judge LLM. 키가 없으면 판단이 필요한 검토는 `LLM_UNAVAILABLE` 로 사람에게 간다. API: 형식 오류 명세 복구. 키가 없으면 `REPAIR_UNAVAILABLE` |

## 검토가 시작되는 곳

- **GitHub 조직 웹훅** → `POST /webhooks/github` (이벤트: `pull_request`, `check_suite`)
  - `pull_request` `opened`·`synchronize`·`reopened`, base 가 기본 브랜치인 PR 만 → head SHA 의 `deploy.yaml` 검토
  - `deploy.yaml` 이 없거나 비었거나 형식이 깨졌으면 검토 대신 **intake**(아래), 같은 레포·head SHA 검토가 있으면 `{"skipped": "already reviewed"}`
  - 새 검토를 만들면 그 PR 의 끝나지 않은 검토(`received`·`reviewing`·`needs_human`·`waiting_ci`)는 `superseded`
  - `pull_request` `closed`(병합 없이) → 그 PR 의 끝나지 않은 검토를 `superseded`(`error` "PR closed"). 승인 화면에서 빠진다. 다시 열면 같은 SHA 도 새로 검토
  - 같은 레포·head SHA 검토는 `failed`·`superseded` 를 빼고 하나뿐이다(0008 부분 unique 인덱스) — 같은 웹훅이 동시에 와도 하나만 만든다.
    `failed` 검토만 있는 SHA 가 다시 오면(발행 실패 뒤 재전송) 새로 검토한다
- `POST /reviews` — 직접 요청 (PR 번호를 모르므로 superseded 대상이 아니다). 파일 없음 404, 빈 파일·형식 오류 422 — intake 를 만들지 않는다.
  `spec_ref.commit` 은 40자 SHA 만 받는다 (병합에 전체 SHA 가 필요하다)

### 명세 없음·빈 명세·형식 오류 (`spec_intakes`, `api/review_api/intake.py`)

웹훅은 `spec_intakes` 에 `processing` 행만 넣고 202 를 돌려준다(GitHub 웹훅 10초 제한). 처리는 응답 뒤에 한다.
조직 웹훅이라 gitops·인프라 레포 PR 도 오므로, **파일이 없는** PR 은 배포된 적 있는 레포(baseline)나 `INTAKE_REPOSITORIES` 에 있는 레포만 intake 로 연다. 파일이 있는데 비었거나 깨졌으면 레포와 무관하게 연다.

| 종류(`kind`) | 처리 | 결과(`status`·`reason`) |
|---|---|---|
| `missing`·`empty` | 그 레포의 최근 baseline(없으면 `DEFAULT_TARGET`)으로 `review_ai.intake.prepare_intake` → PR 브랜치에 `deploy.yaml` 커밋 → synchronize 웹훅이 그 커밋을 **일반 검토**로 시작(`autofix_commit` 아님), `review_id` 로 연결 | `generated`·`GENERATED` |
| `missing`·`empty` (새 앱 — baseline 없음) | PR head 의 Dockerfile·의존성 파일·CI 워크플로·소스를 읽어 근거가 분명한 값만 채운다(`review_ai.intake.analyze`, LLM 없음). 다 채워지면 위와 같이 커밋하고, 커밋 메시지에 값마다 근거 파일을 적는다. **그 PR 의 검토는 pass 여도 `needs_human`(`GENERATED_SPEC_UNVERIFIED`)** — 레포 분석으로는 공개 범위·env·replicas 를 모른다(아래) | `generated`·`GENERATED` |
| `missing`·`empty` (새 앱, 확인 안 된 값 남음) | 추정값은 자동 병합·배포로 이어질 수 있어 커밋하지 않는다. `details` 에 항목별로 레포 분석이 못 채운 이유(DB 드라이버는 있는데 배치 모름, 비밀 이름의 환경변수 등) | `rejected`·`UNVERIFIED` |
| `yaml_error`·`schema_error` | 기본값으로 덮지 않는다. PR head 원문을 비밀을 가려 Claude 에 보내 **형식만** 고치게 하고(`review_ai.intake.repair`), 코드 게이트가 결과 값을 원문과 하나씩 대조한다 — 원문 값을 바꾸거나 지우거나 원문·확인된 값(baseline·레포 분석)·스키마 기본값에 없는 값을 쓰면 버린다. 통과하면 생성과 같이 커밋(`fix:`, 메시지에 바꾼 곳·이유) → 일반 검토. 가린 비밀은 원문 같은 위치에서만 되돌린다 | `repaired`·`REPAIRED` |
| `yaml_error`·`schema_error` (복구 실패) | 게이트 위반은 `details` 에 경로·코드(`VALUE_CONFLICT`·`INVENTED_VALUE`·`DROPPED_VALUE`·`OUTPUT_INVALID`). 비밀을 옮겨야 하면 `MASKED_VALUE`. API 에 키가 없으면 LLM 을 부르지 않는다. 오류 기록은 줄·칸·경로만(원문 조각 없음) | `rejected`·`REPAIR_REJECTED`·`MASKED_VALUE`·`REPAIR_UNAVAILABLE` |

- 그 밖의 거절: 포크 PR(`FORK_PR`), 대상 환경 모름(`NO_TARGET`), 처리 중 새 커밋(`BRANCH_MOVED`), 생성 커밋이 다시 intake 대상(`LOOP_GUARD` — 웹훅·커밋 무한 반복 방지), GitHub·Claude 일시 오류(`failed`·`ERROR` — 새 커밋을 올리면 다시 처리)
- PR 표시: 커밋 상태 `review-service/intake` (pending → success·failure·error). 링크는 `GET /intakes/{id}`. `GET /verify?sha=` 는 검토가 없으면 `intake_failed`·`intake_processing`·`intake_generated`·`intake_repaired` 를 돌려준다(`passed: false`)
- 처리 중 파드가 죽으면 2분 넘은 `processing` 행을 API 가 1분마다 다시 처리한다. 커밋 SHA 는 브랜치를 옮기기 전에 행에 남겨 같은 커밋으로 마저 끝낸다
- **baseline 없이 생성한 명세는 자동 병합하지 않는다** (10/09 sample-app #11 — `network: {}` 명세가 pass → 병합 → gitops ingress 삭제 → ALB 삭제).
  intake 는 생성 커밋 SHA 와 함께 `baseline_used` 를 남기고, 웹훅은 **그 PR(레포·PR 번호)에 baseline 없이 만든 `missing`·`empty` 생성 커밋이 있으면**
  `review.requested` 에 `generated_spec: true` 를 싣는다 → judge 가 findings 와 무관하게 `needs_human`(`GENERATED_SPEC_UNVERIFIED`), AI 자동 수정도 하지 않는다.
  생성 커밋 위에 커밋이 더 올라와도 같다. 판단 근거는 `spec_intakes` 기록뿐 — 명세 내용·커밋 메시지·PR 작성자는 보지 않는다.
  사람이 승인(·수정)하면 이어서 진행하고, 그 뒤 재검사·봇 수정 커밋 재검토에서는 다시 묻지 않는다. `GET /reviews/{id}` 의 `reason_messages` 에 확인할 항목이 나온다.
  baseline 으로 만든 명세와 형식 오류 복구(`repaired` — 값은 원문 대조)는 예전처럼 일반 검토다

## 커밋 상태 `review-service/verify`

검토 결과를 PR head 커밋 상태로 쓴다. 브랜치 보호의 필수 체크로 걸 수 있다
(CI 잡으로 `/verify` 를 부르면 검토가 그 잡이 든 check suite 를 기다려 데드락이 난다 — 커밋 상태는 check suite 밖이다).

| 시점 | state |
|---|---|
| 검토 생성 (API, 워커 수정 커밋의 새 SHA) | `pending` |
| 통과·사람 승인 (CI 대기로 넘어갈 때), **병합 직전 다시** | `success` |
| needs_human · rejected · blocked · failed | `failure` + 사유 |

상태 쓰기가 실패해도 검토는 진행한다. 단 병합 직전 `success` 는 3번 시도해도 못 쓰면 병합하지 않고 `failed`.
토큰에 **Commit statuses: write** 권한이 필요하다. PR 체크 목록의 **Details** 는 진행 화면(`/ui/reviews/{id}`)을 연다.

## 배포 진행 화면 (`/ui/reviews/{id}`, `api/review_api/static/progress.html`)

PR 에서 들어가서 보는 **읽기 전용** 화면이다 (토큰 입력 없음, 승인은 `/ui`). `GET /ui/reviews` 는 최근 검토 20개(superseded 제외) 목록 — PR 링크가 없을 때 데모용 입구.

- `GET /reviews/{id}/progress` 하나만 3초마다 폴링, `final` 이면 30초. 요청한 검토가 superseded 면 `latest_review_id` 로 주소를 바꿔 이어서 본다
- 단계: 명세 생성(intake 가 있을 때) → AI 검토 → 사람 확인(needs_human 이었을 때) → CI 확인 → 병합 → overlay 커밋 → 배포.
  `rejected`·`failed`·`blocked` 는 그 단계에서 `failed` 로 멈추고 뒤는 `skipped`. 배포는 실제 환경 중 하나라도 Healthy 면 완료, Degraded 면 실패
- 단계 시각(`at`)은 아는 것만 — 끝난 단계는 0009 열(`judged_at`·`human_decided_at`·`merged_at`·`gitops_committed_at`), 지금·멈춘 단계는 `updated_at`, 배포는 `deploy_events.received_at`. 0009 전 행과 CI 는 null
- 환경 카드: 대상 환경(deploy.yaml)은 렌더 결과·gitops 커밋·배포 상태, 다른 실제 환경은 배포 상태만(알림이 없으면 "알림 대기"), 계획 환경은 점선 "계획"
- **다른 환경 배포 알림** — 검토 한 건은 대상 환경 하나다. aws 검토의 같은 병합을 로컬 Argo CD 가 배포하면 `/webhooks/argocd` 가 env 로는 검토를 못 찾으므로,
  같은 앱·병합 SHA 검토를 env 와 무관하게 찾아 `deploy_events` 에 그 env 로 남긴다(`{"review_id", "recorded", "cross_env": true}`).
  baseline 갱신·Degraded 판단 사례는 검토 대상 환경일 때만. 같은 알림 중복 판단은 (검토, env, kind, 이미지 태그)

```jsonc
// GET /reviews/rv_20261009_53c71e3c/progress (줄임)
{
  "review": { /* GET /reviews/{id} 와 같은 필드 + reason_messages */ },
  "chain": [{"review_id": "rv_…a1", "status": "superseded", "verdict": "pass", "created_at": "…", "autofix": false},
            {"review_id": "rv_…53c71e3c", "status": "committed", "verdict": "pass", "created_at": "…", "autofix": true}],
  "latest_review_id": null,          // 요청한 검토가 superseded 면 체인 끝 검토
  "intake": null,                    // {intake_id, kind, status, reason, result_commit_sha, created_at}
  "steps": [
    {"key": "review", "label": "AI 검토", "state": "done", "detail": "판정 pass · 자동 수정 커밋 1회", "at": "…"},
    {"key": "ci", "label": "CI 확인", "state": "done", "detail": "CI 통과", "at": null},
    {"key": "merge", "label": "병합", "state": "done", "detail": "병합 3c7ad2e", "at": "…"},
    {"key": "gitops", "label": "overlay 커밋", "state": "done", "detail": "overlay 커밋 0f94208", "at": "…"},
    {"key": "deploy", "label": "배포", "state": "done", "detail": "aws Healthy · local 알림 대기", "at": "…"}
  ],
  "envs": [
    {"env": "aws", "is_target": true, "app_url": "http://…",
     "render": {"status": "committed", "reason": null, "gitops_commit_sha": "0f94208…"},
     "deploy": {"kind": "healthy", "image_tag": "3c7ad2e", "received_at": "…"}},
    {"env": "local", "is_target": false, "app_url": "http://…", "deploy": null},
    {"env": "gcp", "planned": true}
  ],
  "links": {"pr": "https://github.com/…/pull/12", "merge_commit": "https://github.com/…/commit/…",
            "gitops_commit": "https://github.com/Crystal-SBHackathon2026/gitops/commit/…"},
  "findings": [], "decision": {}, "rounds": [],   // 검토 보고서용 — /reviews/{id} 와 같은 수준
  "final": true                                   // 화면이 폴링을 30초로 늦춘다
}
```

## 멈춘 검토 회수 (review sweep, `api/review_api/recovery.py`)

워커가 처리 중이어야 하는 상태(`received`·`reviewing`·`merging`)가 `REVIEW_STALE_AFTER`(10분) 넘게 그대로면 API 가 1분마다 한 행씩
조건부 UPDATE(`SKIP LOCKED`)로 가져가 다시 보낸다 — API 파드가 여럿이어도 한 곳만. 가져갈 때마다 `recover_count` 를 올리고 3번을 넘으면 `failed` + verify `failure`.

| 상태 | 회수 |
|---|---|
| `received` | `review.requested` 다시 발행 (spec_ref 커밋의 deploy.yaml 로 다시 만든다) |
| `reviewing` | `received` 로 되돌리고 다시 발행. 워커가 체크포인트를 보고 멈춘 노드부터 이어서·처음부터·사람 결정 대기로 되돌린다 |
| `merging` + `merge_sha` | `review.resumed`(`retry_overlay`) — 워커가 `commit_overlay` 만 다시 한다 (병합 뒤 gitops 커밋 예외는 `failed` 가 아니라 `merging` 으로 남는다) |
| `merging`, `merge_sha` 없음 | GitHub 에서 PR 병합 여부 확인 — 병합됐으면 `merge_sha` 기록 후 `retry_overlay`, 아니면 `waiting_ci` 로 되돌리고 CI 재확인 |

`needs_human`·`waiting_ci` 는 사람·CI 를 기다리는 상태라 회수하지 않는다.

## 헬스체크

- API `GET /healthz` — 프로세스만 본다 (ALB 헬스체크, livenessProbe). `GET /readyz` — DB `SELECT 1`·Kafka 브로커 연결, 실패하면 503 (readinessProbe)
- 워커 — 메인 루프가 poll 마다(1초) `/tmp/worker-alive` 를 갱신한다. 메시지 처리 중에는 30초마다, `max_poll_interval` 까지.
  livenessProbe: `find /tmp/worker-alive -mmin -2 | grep -q .`

## 지표 (Prometheus)

`prometheus_client` 하나만 쓴다. API 는 `GET /metrics`(토큰 없이), 워커는 `:9100/metrics`. 접두사 `review_`.
라벨은 값 종류가 적은 것만 — `review_id`·커밋 SHA·레포 경로는 넣지 않는다 (커밋 단위는 Grafana 커밋 타임라인, 업무 DB).
지표 기록이 실패해도 요청·검토 처리에는 영향이 없다.

| 이름 | 타입 | 라벨 | 뜻 |
|---|---|---|---|
| `review_http_requests_total` | counter | route, method, status | API 요청. route 는 경로 템플릿, 없는 경로는 `unmatched`. `/metrics`·`/healthz`·`/readyz` 제외 |
| `review_http_request_duration_seconds` | histogram | route, method | API 처리 시간 |
| `review_webhook_events_total` | counter | event, result | pull_request·check_suite·argocd / started·intake·skipped·duplicate·rejected·error |
| `review_kafka_publish_total` | counter | topic, result | ok·timeout·error (API·워커 각자) |
| `review_sweep_recovered_total` | counter | from_status | review sweep 이 회수한 검토 |
| `review_sweep_failed_total` | counter | | 회수 3번을 넘어 failed |
| `review_intake_sweep_recovered_total` | counter | | intake sweep 이 다시 처리한 intake |
| `review_reviews` | gauge | app, env, status | 최근 1일 안에 바뀐 검토 수 (scrape 때 업무 DB, 15초 캐시, 실패하면 마지막 값) |
| `review_needs_human_oldest_seconds` | gauge | app, env | 가장 오래 기다린 needs_human 의 대기 시간 (updated_at 기준) |
| `review_worker_messages_total` | counter | topic, kind, result | kind: requested·human_decision·ci_completed·retry_overlay / result: processed·skipped·invalid·error |
| `review_worker_node_duration_seconds` | histogram | node | 그래프 노드 처리 시간 (interrupt 대기는 빼고) |
| `review_worker_node_errors_total` | counter | node | 노드 예외 |
| `review_worker_verdicts_total` | counter | app, env, verdict | pass·fix·needs_human (재검사 회차마다) |
| `review_worker_needs_human_reasons_total` | counter | reason | needs_human 사유 코드 |
| `review_worker_finished_total` | counter | app, env, status | committed·blocked·rejected·failed·superseded |
| `review_worker_llm_calls_total` | counter | purpose, result | judge / ok·timeout·error (캐시 적중 제외) |
| `review_worker_llm_duration_seconds` | histogram | purpose | LLM 호출 시간 |
| `review_worker_llm_tokens_total` | counter | purpose, type | input·output 토큰 |
| `review_worker_consumer_lag` | gauge | topic, partition | 30초마다 끝 오프셋 - 커밋 오프셋 |
| `review_worker_last_heartbeat_timestamp` | gauge | | 생존 파일을 마지막으로 갱신한 시각 |

수집 설정(ServiceMonitor)과 대시보드는 gitops `apps/review-service/monitoring/`. 커밋 타임라인은 `reviews` 의 단계별 시각
(`judged_at`·`human_decided_at`·`merged_at`·`gitops_committed_at`, 마이그레이션 0009)과 `deploy_events`·`spec_intakes` 로 그린다.

## 상태 흐름 (`reviews.status`)

```
received → reviewing ─┬─ needs_human ─┬─ (승인, 적용할 수정 없음) → waiting_ci
                      │               ├─ (승인 + edited_ops 또는 권장값) → reviewing → …재검사
                      │               └─ (거절) → rejected
                      ├─ superseded   (고쳐서 통과 → PR 브랜치에 수정 커밋 → 그 커밋을 새 검토로, superseded_by)
                      └─ waiting_ci ─┬─ (CI success) → merging → committed | blocked
                                     └─ (CI 실패) → failed
```

- **CI 대기:** waiting_ci 로 바꾼 뒤 GitHub check-suites 를 먼저 조회해 이미 끝났으면 기다리지 않는다(gitops#9).
  안 끝났으면 `check_suite` 웹훅으로 재개하고, 재개 때 다시 조회해 다른 suite 가 남았으면 계속 기다린다.
  CI 결론은 언제나 check-suites 조회로 정한다 — 조회가 실패하면 웹훅 결론으로 병합하지 않고 다시 기다린다.
  병합 직전(`confirm_ci`, guard_overlay 앞)에 한 번 더 조회해 전체 success 가 아니면 waiting_ci 로 돌아간다.
- **AI 수정 커밋:** 고친 것이 있으면(`applied_ops`, 사람 수정 포함) 원본 deploy.yaml 에 적용해 PR 브랜치에 커밋한다.
  새 커밋은 `autofix_commit=True` 로 다시 검토하고, 거기서 또 fix 면 needs_human(LOOP_EXHAUSTED) 이다.
  포크 PR 은 커밋할 수 없어 failed. 커밋한 deploy.yaml 은 YAML 을 다시 쓰므로 원래 주석은 사라진다.
- 그래프 오류·PR head 불일치·병합 실패도 `failed` 이고 원인은 `error` 열에 남는다.
- 사람 승인 시 입력하지 않은 항목은 `decision.recommendations`의 검증된 권장값을 적용하고 재검사한다.
  입력한 값이 우선이며, 기존 명세 그대로 승인하려면 `use_recommendations: false`를 명시한다. 권장값이 없으면 값 없는 승인은 그대로 승인이다. [응답 기본값 안내](ai/docs/human-recommendations.md).
- 종료 지점(거절·AI 수정 커밋·사람 승인 뒤 committed)과 Argo CD Degraded 에서 판단 사례를 `review_cases` 에 남기고, 다음 검토가 같은 규칙에 걸리면 근거 문서로 붙인다. 기록 실패는 검토 결과를 바꾸지 않는다. [판단 사례](ai/docs/review-ai.md#판단-사례-review_cases)
- `commit_overlay`(성진님, `worker/review_worker/commit_overlay.py`)가 원본 명세 + `applied_ops` → overlay 를 gitops main 에 커밋한다. 렌더러 blocking 경고면 커밋하지 않고 `blocked`. gitops ref 충돌은 main 을 다시 읽어 최대 5회.

### 배포 실패 분석

Argo CD 오류 수신, 별도 Kafka 진단, 다음 요청의 실패 사례 비교와 운영자 UI는 [배포 실패 분석 문서](ai/docs/deployment-failure-analysis.md)를 참고하세요. 신규 송신 계약은 [JSON Schema](api/schema/deployment.result.v1.schema.json)에 있습니다.
