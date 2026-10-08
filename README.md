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
| `GITHUB_TOKEN` | API·워커 | API 는 deploy.yaml 읽기, 워커는 PR 병합. 없으면 미인증(시간당 60회) |
| `GITHUB_WEBHOOK_SECRET` | API | `/webhooks/github` HMAC 검증. 없으면 웹훅을 503 으로 거절 |
| `GITHUB_CI_APP_SLUG` | API | 이 GitHub App 의 `check_suite` 만 CI 결과로 본다. 기본 `github-actions`, 빈 값이면 전부 |
| `ARGOCD_WEBHOOK_TOKEN` | API | 있으면 `/webhooks/argocd` 가 `Authorization: Bearer <토큰>` 을 확인한다 |
| `ANTHROPIC_API_KEY` `REVIEW_LLM_MODEL` | 워커 | judge LLM. 키가 없으면 판단이 필요한 검토는 `LLM_UNAVAILABLE` 로 사람에게 간다 |

## 상태 흐름 (`reviews.status`)

```
received → reviewing ─┬─ needs_human ─┬─ (승인) → waiting_ci
                      │               ├─ (승인 + edited_ops) → reviewing → …재검사
                      │               └─ (거절) → rejected
                      └─ waiting_ci ─┬─ (CI success) → merging → committed | blocked
                                     └─ (CI 실패) → failed
```

그래프 오류·PR head 불일치·병합 실패도 `failed` 이고 원인은 `error` 열에 남는다.
`commit_overlay` 는 아직 스텁이라 병합 뒤 `blocked`(`COMMIT_OVERLAY_NOT_IMPLEMENTED`)로 끝난다.
