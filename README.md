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
| `ai/` | AI 판단 쪽 — 명세 모델, 규칙 목록, 샘플, 평가셋 |

파이프라인 쪽 디렉터리는 구조가 정해지면 추가한다.
