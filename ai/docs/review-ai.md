# AI 판단 모듈 (review_ai) — 파이프라인 연결 안내

검토 그래프의 판단 노드 3개와 그 주변(마스킹·Kafka 메시지·overlay 렌더러·평가셋)이다.
파이프라인 담당은 아래 **연결 지점**만 보면 된다. 판단 노드는 Kafka·Qdrant·API 키 없이도 단독으로 돌고 테스트된다.

```
static_check → retrieve_evidence → judge ─┬─ pass / needs_human → 끝
     ▲                                     └─ fix → apply_patch ─┘ (최대 2회)
```

## 연결 지점

| 쓰는 곳 | 가져다 쓸 것 | 비고 |
|---|---|---|
| Review API | `messages.build_review_requested(spec, review_id=, spec_ref=, requested_by=, requested_at=)` | baseline 을 떼고 `mask_spec()` 한 뒤 `spec_sha256` 계산. `ReviewRequested` 모델이 평문 비밀·baseline·해시 불일치를 거절한다 |
| Review API | `spec.deploy_spec.DeploySpec` / `schema/deploy_spec.schema.json` | 형식 오류 → 422 |
| 워커 그래프 | `static_check.make_static_check()` | `findings` 만 반환 |
| 워커 그래프 | `retrieval.make_retrieve_evidence(retriever)` | `FileRetriever()`(벡터 DB 없음) 또는 `QdrantRetriever(client, FastEmbedder())` |
| 워커 그래프 | `judge.node.make_judge(llm)` | `CachedLLM(ClaudeLLM())` 권장. `llm=None` 이면 LLM_UNAVAILABLE |
| 워커 재시도 실패 시 | `judge.node.judge_unavailable(state, error=...)` | RetryPolicy 소진 뒤 이 결과로 State 를 채우고 계속. 원인은 `decision.llm.error` |
| apply_patch | `verdict.round_snapshot(state)` · `patching.apply_ops(spec, patch["ops"])` | 참고 구현: `graph.apply_patch` |
| 커밋 단계 | `verdict.applied_ops(final)` · `patching.apply_ops(원본, ops)` · `overlay.render_overlay(spec)` → `files`·`warnings` | **spec_ref 로 앱 레포에서 읽은 원본**에 `applied_ops(final)` 를 적용해 렌더링한다. ⚠️ `patch["ops"]` 가 아니다 — apply_patch 가 patch 를 rounds 로 옮기고 비우므로 고쳐서 통과한 최종 State 는 `patch=None` 이고, ops 는 `rounds[*].patch.ops` 에 회차 순서대로 있다. `applied_ops == []` 면 수정 없이 통과(원본 그대로). Kafka 사본은 가려져 있어 렌더러가 거절한다. 회차별 diff 는 `rounds[i].patch.files` 에 있다 |
| 단독 실행·평가 | `graph.run_graph(initial_state(spec, review_id=), llm=, retriever=)` | 최종 State 에 `status`·`applied_ops`·`patched`(고친 적 있음) 를 더해 돌려준다. 고쳐서 통과 = `status == "pass" and patched`. `scripts/run_eval.py` 가 이걸 쓴다 |

## 패치 게이트

LLM 이 낸 패치는 아래를 모두 통과해야 `fix` 가 된다. 하나라도 어기면 패치를 버리고 `needs_human`(PATCH_OUT_OF_SCOPE)이다.

1. 대상은 autofix allowed finding 만, ops 는 대상 규칙의 허용 경로(`judge/prompt.py` `RULE_PATCH_PATHS`) 아래만 — env·이미지·시크릿·네트워크는 어떤 규칙으로도 못 바꾼다
2. 실제 명세에 적용되고 DeploySpec 형식을 통과한다 (allowed_cidrs 는 CIDR 형식 검사)
3. DB 엔진·배치 변경은 데이터가 없고, `engine_policy: allow_convert` 이거나 대상이 DB-002 일 때만
4. persistent 볼륨을 없애거나 비영속으로 바꾸거나 줄이지 않는다
5. 비밀처럼 보이는 env 를 새로 넣지 않는다
6. 적용한 명세를 다시 static_check 하면 대상 finding 이 사라지고 새 finding 이 생기지 않는다

6번 때문에 루프 안 재검사는 대부분 첫 회차에 끝난다. LOOP_EXHAUSTED 는 안전장치로 남아 있다.
LLM 의 자유 텍스트(why·extra_opinions)는 비밀처럼 보이는 부분을 가리고, 개수·길이를 제한한다. 명세는 `< > &` 를 이스케이프해 프롬프트 구분 태그를 흉내 내지 못하게 한다.

## 비밀 판별 기준 (`secrets_pattern.py`)

SEC-001·mask_spec·패치 게이트·LLM 출력 검사가 같은 기준을 쓴다.
- 이름: `_` 로 나눈 토큰에 PASSWORD·PASS·PWD·SECRET·TOKEN·CREDENTIAL·APIKEY·DSN·AUTHORIZATION·BEARER, 또는 `*_KEY` 로 끝남
- 값(어느 필드든): `scheme://[user]:pw@`, `user:pw@host`, `?password=`, AWS 키 ID, GitHub·Slack·`sk-` 토큰, PEM 개인키, `Bearer …`, 이미 가린 `***MASKED***`
- 이름이 비밀이면 값의 타입(숫자 등)과 무관하게 가린다

노드 에러: 일시적 오류는 `errors.TransientError` 를 raise(파이프라인 RetryPolicy), 근거 부족·인용 오류·모델 거절은 정상 반환 + 사유 코드, 그 밖은 버그로 그대로 올라간다.

## 제안서(State 스키마)와 달라진 점

| 항목 | 제안서 | 구현 | 이유 |
|---|---|---|---|
| `Finding.category` | 4개 | `runtime` 추가 | readiness·아키텍처 규칙 (결정 #5) |
| `Finding` | — | `title`·`irreversible` 추가 | decide_verdict 가 규칙 목록을 다시 읽지 않게 |
| `Patch` | `files: [{path, diff}]` | **`ops`(deploy_spec JSON Patch)가 정본**, `files` 는 ops 를 overlay 로 렌더링한 diff. `kind` 는 지금 항상 `config` | 명세를 고쳐야 재검사·형식 검사가 가능. overlay 는 명세에서 결정적으로 나온다 |
| `spec_ref` | str | `{repository, commit, path}` | Kafka 메시지와 같은 모양 |
| 사유 코드 | 7개 | `PATCH_MISSING` 추가 | 고칠 수 있는 finding 인데 LLM 이 패치를 안 낸 경우 |
| low finding | — | 사람 확인 조건에서 제외 (`verdict.EXCLUDE_LOW_FROM_HUMAN`) | 결정 #6. 단 LLM 출력이 틀리면(CITATION_INVALID) low 만 있어도 needs_human |
| low 만 있을 때 | LLM 호출 | **LLM 호출 안 함** | 매 배포 경고 설명에 비용을 쓰지 않는다. 설명은 규칙 제목 |
| 데이터 있는 SQLite + replicas 2 | needs_human (IRREVERSIBLE) | **fix (replicas → 1)** | replicas 를 줄이는 건 되돌릴 수 있다. 엔진 전환 패치는 데이터가 있으면 범위 밖으로 막힌다(`sqlite-with-data-llm-converts-engine` 케이스) |

## overlay 렌더러 (결정 #7: 검토 서비스 코드가 만든다)

- 입력 `deploy_spec` → 출력 `apps/<앱>/overlays/<환경>/{kustomization,ingress,pvc-*}.yaml`. 디스크에 쓰지 않고 dict 로 준다.
- base 의 Rollout 컨테이너 0번을 **통째로** 바꾸고(replicas·env·probe·resources·volumeMounts), Service targetPort, 이미지 태그 **+ digest** 를 overlay 별로 고정한다.
- 지금 gitops 의 sample-app aws·gcp·local overlay 를 `kubectl kustomize` 결과 기준으로 그대로 재현한다(테스트). 차이는 의도한 두 가지 — 이미지 digest 고정, `terminationGracePeriodSeconds` 명시.
- 시크릿은 `secretKeyRef`(`<앱>-secrets`)로만 렌더링한다. 값은 넣지 않는다.
- overlay 로 못 만드는 것(관리형 DB, 버킷, Secret 값 생성, 로컬·GCP 의 allowed_cidrs)은 `warnings` 로 돌려준다.

```bash
.venv/bin/python scripts/render_overlay.py samples/01-pass-sample-app-aws.yaml
```

## 근거 문서 (knowledge/)

S3 `review-docs` 버킷과 같은 구조다: `rules/{any,aws,gcp,local}/<ruleId>.md`, `incidents/K-*.md`(oneaction 리허설 카드 13장), `guides/*.md`.
문서의 `## ` 섹션 하나가 청크다. P0 규칙 11개 전부 문서가 있고, 환경별 청크 수는 테스트로 30개 이상을 유지한다.

- 규칙 문서는 ruleId 정확 매칭(점수 1.0)으로 찾고, 사례·가이드는 의미 검색(dense cosine, 0.5 미만 버림)으로 보탠다.
- 임베딩은 로컬 `paraphrase-multilingual-MiniLM-L12-v2`(fastembed, 키 없음). 실측해 보니 무관한 사례가 0.34~0.43 으로 나와 판정은 ruleId 매칭에 기대고 의미 검색은 보조로만 쓴다.

```bash
docker run -d -p 6333:6333 qdrant/qdrant
.venv/bin/python scripts/index_knowledge.py --qdrant-url http://localhost:6333
```

## 평가셋 (eval/cases.yaml)

샘플 10개 + 제안서의 환각 케이스 10개. `reviewer` 자리에는 평가 대상 LLM 이, 나머지 자리에는 일부러 틀리는 가짜 LLM 이 들어가 코드 게이트가 잡는지 본다.

```bash
.venv/bin/python scripts/run_eval.py                                  # 가짜 LLM, 키 없이
ANTHROPIC_API_KEY=... .venv/bin/python scripts/run_eval.py --llm claude --repeat 3
.venv/bin/python scripts/run_eval.py --retriever qdrant               # 로컬 임베딩 + 메모리 Qdrant
```

지표: 기대 verdict 일치율 · 인용 유효율 · pass 기대 케이스 오탐 · LLM 호출 수·토큰·시간. 결과는 `eval/reports/`(git 제외).

## 개발

```bash
cd ai && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q --cov=review_ai
.venv/bin/python scripts/validate_samples.py
```
