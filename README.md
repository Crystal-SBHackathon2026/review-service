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
| `GITHUB_TOKEN` | API·워커 | API 는 deploy.yaml 읽기와 명세 생성 커밋(PR 브랜치 Contents 쓰기)·PR 커밋 상태(**Commit statuses 쓰기**, 없으면 표시만 빠진다). 워커는 CI 상태 조회·AI 수정 커밋·PR 병합·gitops overlay 커밋이라 앱 레포·gitops Contents·Pull requests **쓰기** 권한이 필요하다 (`oneaction/gitops-token`) |
| `GITOPS_REPO` | 워커 | overlay 를 커밋할 gitops 레포. 기본 `Crystal-SBHackathon2026/gitops` |
| `GITHUB_WEBHOOK_SECRET` | API | `/webhooks/github` HMAC 검증. 없으면 웹훅을 503 으로 거절 |
| `GITHUB_CI_APP_SLUG` | API·워커 | 이 GitHub App 의 `check_suite` 만 CI 결과로 본다. 기본 `github-actions`, 빈 값이면 전부 |
| `DEFAULT_TARGET` | API | baseline 이 없는 레포에 명세를 만들 대상 `env/region` (예 `aws/ap-northeast-2`). 없으면 그런 레포는 `NO_TARGET` |
| `INTAKE_REPOSITORIES` | API | `owner/repo,…` — baseline 이 없어도 `deploy.yaml` 없음을 intake 로 볼 레포(새 앱). 웹훅이 조직 단위라 목록·baseline 에 없는 레포는 예전처럼 skip |
| `REVIEW_API_PUBLIC_URL` | API | PR 커밋 상태의 링크(`/intakes/{id}`) 앞부분. 없으면 링크 없이 표시 |
| `ARGOCD_WEBHOOK_TOKEN` | API | 있으면 `/webhooks/argocd` 가 `Authorization: Bearer <토큰>` 을 확인한다 |
| `ANTHROPIC_API_KEY` `REVIEW_LLM_MODEL` | 워커 | judge LLM. 키가 없으면 판단이 필요한 검토는 `LLM_UNAVAILABLE` 로 사람에게 간다 |

## 검토가 시작되는 곳

- **GitHub 조직 웹훅** → `POST /webhooks/github` (이벤트: `pull_request`, `check_suite`)
  - `pull_request` `opened`·`synchronize`·`reopened`, base 가 기본 브랜치인 PR 만 → head SHA 의 `deploy.yaml` 검토
  - `deploy.yaml` 이 없거나 비었거나 형식이 깨졌으면 검토 대신 **intake**(아래), 같은 레포·head SHA 검토가 있으면 `{"skipped": "already reviewed"}`
  - 새 검토를 만들면 그 PR 의 끝나지 않은 검토(`received`·`reviewing`·`needs_human`·`waiting_ci`)는 `superseded`
- `POST /reviews` — 직접 요청 (PR 번호를 모르므로 superseded 대상이 아니다). 파일 없음 404, 빈 파일·형식 오류 422 — intake 를 만들지 않는다

### 명세 없음·빈 명세·형식 오류 (`spec_intakes`, `api/review_api/intake.py`)

웹훅은 `spec_intakes` 에 `processing` 행만 넣고 202 를 돌려준다(GitHub 웹훅 10초 제한). 처리는 응답 뒤에 한다.
조직 웹훅이라 gitops·인프라 레포 PR 도 오므로, **파일이 없는** PR 은 배포된 적 있는 레포(baseline)나 `INTAKE_REPOSITORIES` 에 있는 레포만 intake 로 연다. 파일이 있는데 비었거나 깨졌으면 레포와 무관하게 연다.

| 종류(`kind`) | 처리 | 결과(`status`·`reason`) |
|---|---|---|
| `missing`·`empty` | 그 레포의 최근 baseline(없으면 `DEFAULT_TARGET`)으로 `review_ai.intake.prepare_intake` → PR 브랜치에 `deploy.yaml` 커밋 → synchronize 웹훅이 그 커밋을 **일반 검토**로 시작(`autofix_commit` 아님), `review_id` 로 연결 | `generated`·`GENERATED` |
| `missing`·`empty` (새 앱 — baseline 없음) | PR head 의 Dockerfile·의존성 파일·CI 워크플로·소스를 읽어 근거가 분명한 값만 채운다(`review_ai.intake.analyze`, LLM 없음). 다 채워지면 위와 같이 커밋하고, 커밋 메시지에 값마다 근거 파일을 적는다 | `generated`·`GENERATED` |
| `missing`·`empty` (새 앱, 확인 안 된 값 남음) | 추정값은 자동 병합·배포로 이어질 수 있어 커밋하지 않는다. `details` 에 항목별로 레포 분석이 못 채운 이유(DB 드라이버는 있는데 배치 모름, 비밀 이름의 환경변수 등) | `rejected`·`UNVERIFIED` |
| `yaml_error`·`schema_error` | 기본값으로 덮지 않는다. 자동 복구(LLM)는 아직 없다. 오류는 줄·칸·경로만 남긴다(원문 조각 없음) | `rejected`·`REPAIR_UNAVAILABLE` |

- 그 밖의 거절: 포크 PR(`FORK_PR`), 대상 환경 모름(`NO_TARGET`), 처리 중 새 커밋(`BRANCH_MOVED`), 생성 커밋이 다시 intake 대상(`LOOP_GUARD` — 웹훅·커밋 무한 반복 방지), GitHub 오류(`failed`·`ERROR`)
- PR 표시: 커밋 상태 `review-service/intake` (pending → success·failure·error). 링크는 `GET /intakes/{id}`. `GET /verify?sha=` 는 검토가 없으면 `intake_failed`·`intake_processing`·`intake_generated` 를 돌려준다(`passed: false`)
- 처리 중 파드가 죽으면 2분 넘은 `processing` 행을 API 가 1분마다 다시 처리한다. 커밋 SHA 는 브랜치를 옮기기 전에 행에 남겨 같은 커밋으로 마저 끝낸다

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
- **AI 수정 커밋:** 고친 것이 있으면(`applied_ops`, 사람 수정 포함) 원본 deploy.yaml 에 적용해 PR 브랜치에 커밋한다.
  새 커밋은 `autofix_commit=True` 로 다시 검토하고, 거기서 또 fix 면 needs_human(LOOP_EXHAUSTED) 이다.
  포크 PR 은 커밋할 수 없어 failed. 커밋한 deploy.yaml 은 YAML 을 다시 쓰므로 원래 주석은 사라진다.
- 그래프 오류·PR head 불일치·병합 실패도 `failed` 이고 원인은 `error` 열에 남는다.
- 사람 승인 시 입력하지 않은 항목은 `decision.recommendations`의 검증된 권장값을 적용하고 재검사한다.
  입력한 값이 우선이며, 기존 명세 그대로 승인하려면 `use_recommendations: false`를 명시한다. 권장값이 없으면 값 없는 승인은 그대로 승인이다. [응답 기본값 안내](ai/docs/human-recommendations.md).
- `commit_overlay`(성진님, `worker/review_worker/commit_overlay.py`)가 원본 명세 + `applied_ops` → overlay 를 gitops main 에 커밋한다. 렌더러 blocking 경고면 커밋하지 않고 `blocked`. gitops ref 충돌은 main 을 다시 읽어 최대 5회.
