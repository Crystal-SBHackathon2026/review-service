# AI 판단 모듈 (review_ai) — 파이프라인 연결 안내

검토 그래프의 판단 노드 3개와 그 주변(마스킹·Kafka 메시지·overlay 렌더러·평가셋)이다.
파이프라인 담당은 아래 **연결 지점**만 보면 된다. 판단 노드는 Kafka·Qdrant·API 키 없이도 단독으로 돌고 테스트된다.

```
START ─┬────────────────────▶ static_check → retrieve_evidence → judge ─┬─ pass / needs_human → 끝
       └─ apply_human_edits ─▶ ▲                                       └─ fix → apply_patch ─┘ (최대 2회)
          (승인 + edited_ops)
```

## 설치

워커·Review API 는 `review_ai` 를 패키지로 설치해 import 한다. State 는 `review_ai.state.ReviewState` 하나만 쓴다.

```bash
pip install -e ./ai               # 개발 — 소스의 catalog/·knowledge/ 를 그대로 읽는다
pip install "./ai[qdrant]"        # 이미지 빌드 — catalog/·knowledge/ 를 패키지 안(review_ai/_data/)에 복사한다
```

- Python 3.13 이상. 기본 의존성은 pydantic·pyyaml·langgraph·anthropic 이고, `QdrantRetriever` 를 쓸 때만 `[qdrant]`(qdrant-client·fastembed)가 필요하다.
- 연결 흐름 전체(메시지 → Kafka 왕복 → 그래프 → `applied_ops` → 렌더링)는 `tests/test_pipeline_flow.py` 가 샘플 10개로 고정한다. 워커를 붙일 때 이 테스트를 본보기로 쓰면 된다.

## 연결 지점

명세 미전달·빈 YAML은 `preparation.prepare_spec` 또는 `preparation.prepare_and_review`로 권장 명세를 생성할 수 있다. 기존 판정 노드 앞에서 쓰는 별도 진입점이며, 저장·커밋은 파이프라인 책임이다. [입력·응답·파이프라인 연결 안내](spec-preparation.md).

| 쓰는 곳 | 가져다 쓸 것 | 비고 |
|---|---|---|
| Review API | `messages.build_review_requested(spec, review_id=, spec_ref=, requested_by=, requested_at=, autofix_commit=False)` | baseline 을 떼고 `mask_spec()` 한 뒤 `spec_sha256` 계산. `ReviewRequested` 모델이 평문 비밀·baseline·해시 불일치를 거절한다. **검토할 커밋이 워커가 `applied_ops` 를 커밋한 것(봇 커밋)이면 `autofix_commit=True`** — 워커는 `initial_state(..., autofix_commit=message.autofix_commit)` 로 넘긴다 |
| Review API | `spec.deploy_spec.DeploySpec` / `schema/deploy_spec.schema.json` | 형식 오류 → 422 |
| 워커 그래프 | `static_check.make_static_check()` | `findings` 만 반환 |
| 워커 그래프 | `retrieval.make_retrieve_evidence(retriever)` | `FileRetriever()`(벡터 DB 없음) 또는 `QdrantRetriever(client, FastEmbedder())`. 워커는 `CompositeRetriever(FileRetriever(), CaseRetriever(repo))` — 규칙 문서 뒤에 업무 DB 판단 사례(아래 [판단 사례](#판단-사례-review_cases)) |
| 워커 종료 지점·Argo CD Degraded | `cases.case_from_state(state, human=)` · `cases.case_from_review(row, "deploy_degraded")` → `repo.insert_case(**case)` | 판단이 필요했던 finding(low 제외)이 없으면 `None`. 기록 실패는 경고 로그만 — 검토 결과를 바꾸지 않는다 |
| 워커 그래프 | `judge.node.make_judge(llm)` | `CachedLLM(ClaudeLLM())` 권장. `llm=None` 이면 LLM_UNAVAILABLE. `decision`·`patch`·**`status`(= verdict)** 를 쓴다 — 그래프를 직접 조립해도 status 가 채워진다. `autofix_commit` 이거나 `human_decision` 이 있으면 fix 대신 needs_human(LOOP_EXHAUSTED). `generated_spec` 이고 `human_decision` 이 없으면 findings 와 무관하게 needs_human(GENERATED_SPEC_UNVERIFIED) — `judge.node.spec_unverified` |
| 워커 재시도 실패 시 | `judge.node.judge_unavailable(state, error=...)` | RetryPolicy 소진 뒤 이 결과로 State 를 채우고 계속. 원인은 `decision.llm.error` |
| apply_patch | `verdict.round_snapshot(state)` · `patching.apply_ops(spec, patch["ops"])` | 참고 구현: `graph.apply_patch` |
| 커밋 단계 | `verdict.applied_ops(final)` · `patching.apply_ops(원본, ops)` · `overlay.render_overlay(spec)` → `files`·`warnings`·`blocking` | **spec_ref 로 앱 레포에서 읽은 원본**에 `applied_ops(final)` 를 적용해 렌더링한다. ⚠️ `patch["ops"]` 가 아니다 — apply_patch 가 patch 를 rounds 로 옮기고 비우므로 고쳐서 통과한 최종 State 는 `patch=None` 이고, ops 는 `rounds[*].patch.ops` 에 회차 순서대로 있다. `applied_ops == []` 면 수정 없이 통과(원본 그대로). Kafka 사본은 가려져 있어 렌더러가 거절한다. 회차별 diff 는 `rounds[i].patch.files` 에 있다. `rendered.blocking` 이 비어 있지 않으면 커밋하지 말고 `blocked` 로 돌려준다 |
| 커밋 노드 (`commit_overlay`, 배포 담당) | `state.DeployResult` → State 의 `deploy_result` | `{status: committed \| blocked, commit_sha, reason}`. verdict 가 pass 일 때만 쓴다. `status`(verdict 값)와 섞지 않는다. push 실패처럼 재시도할 오류는 `errors.TransientError`, DB·버킷이 없어 멈추는 건 `blocked` 로 정상 반환 |
| 사람 확인 재개 (승인 API·워커) | `state.HumanDecision` → State 의 `human_decision`, `status` 에 `rejected` · `graph.apply_human_edits` · `graph.check_edited_ops(spec, ops)` | `{decision: approved \| rejected, approver, edited_ops, use_recommendations}`. 승인이면 먼저 `recommendations.resolve_human_decision` 이 미입력 항목을 `decision.recommendations` 로 채운다(`use_recommendations: false` 면 채우지 않음, [권장값 안내](human-recommendations.md)). 채운 뒤 `edited_ops` 없음 → `commit_overlay`, 있음 → **`apply_human_edits` 노드 → `static_check`** 부터 재검사, 거절 → `status=rejected` 로 종료. `edited_ops` 는 원본이 아니라 **멈춘 State 의 `deploy_spec`(AI 가 고친 회차 반영) 기준**이다. `apply_human_edits` 가 needs_human 회차와 사람 ops 를 `rounds` 에 남기므로 커밋 노드는 그대로 `applied_ops(state)` 를 쓰면 사람 수정까지 들어간다. 사람이 고친 뒤 남은 문제는 AI 가 다시 고치지 않고 needs_human(LOOP_EXHAUSTED). 승인 API 는 재개 전에 `check_edited_ops` 로 검사해 422 를 돌려주면 워커가 재개 중에 실패하지 않는다. `/baseline` 아래는 고칠 수 없다(관측 사실을 바꿔 DB-001 등을 우회하는 것을 막고, 원본엔 baseline 이 없어 커밋 단계에서 적용되지 않음) |
| 결과 저장 (업무 DB·결과 화면) | 최종 State 의 `status`·`decision`·`findings`·`rounds` | `status` 는 `running \| pass \| fix \| needs_human \| rejected`. 고쳐서 통과하면 최종 `findings`·`decision.items`·`retrieved_docs` 는 재검사 결과라 비어 있다. **무엇을 왜 고쳤는지는 `rounds[i]`** 에 있다 — `{round, finding_ids, findings, verdict, reasons, items(why·cited_rule_ids), doc_ids, patch(ops·files)}`. 사람이 고친 회차는 `verdict: needs_human` + `human: {decision, approver}` 가 더 붙는다 |
| 단독 실행·평가 | `graph.run_graph(initial_state(spec, review_id=), llm=, retriever=)` | 최종 State 에 `applied_ops`·`patched`(고친 적 있음) 를 더해 돌려준다. 멈춘 State 에 `human_decision` 을 넣어 다시 부르면 재개 경로를 탄다. 고쳐서 통과 = `status == "pass" and patched`. `scripts/run_eval.py` 가 이걸 쓴다 |

## 패치 게이트

LLM 이 낸 패치는 아래를 모두 통과해야 `fix` 가 된다. 하나라도 어기면 패치를 버리고 `needs_human`(PATCH_OUT_OF_SCOPE)이다.

1. 대상은 autofix allowed finding 만. 적용 전후 명세에서 **실제로 바뀐 말단 필드**가 전부 대상 규칙의 허용 필드(`judge/prompt.py` `RULE_PATCH_PATHS`, `*` = 리스트 인덱스나 env 이름) 아래여야 한다 — 객체를 통째로 replace 해도 다른 필드가 바뀌면 버린다. 이미지·baseline 은 어떤 규칙으로도 못 바꾸고, env 는 SEC-004 대상 항목을 지우는 것만 된다(값을 바꾸거나 다른 env 를 건드리면 버린다). secrets 는 이름이 겹친 항목을 지우는 것만(SEC-003), `network.ingress.allowed_cidrs` 는 전체 대역 항목을 지우는 것만(NET-002) 된다 — 좁은 대역을 지우면 오히려 넓어진다
2. op 경로도 허용 필드와 겹쳐야 하고, op 값에 `***MASKED***` 가 없어야 한다 — State 명세는 가린 사본이라, 가린 값을 다시 쓰는 op 는 여기선 변화가 없어도 원본에 적용하면 실제 값을 덮는다
3. 실제 명세에 적용되고 DeploySpec 형식을 통과한다 (allowed_cidrs 는 CIDR 형식 검사)
4. DB 엔진·배치 변경은 데이터가 없고, `engine_policy: allow_convert` 이거나 대상이 DB-002 일 때만
5. persistent 볼륨을 없애거나 비영속으로 바꾸거나 줄이지 않는다
6. **지워서 고치지 않는다** — DB 를 없애거나(engine none) 외부 DB(external)로 돌리거나, 버킷을 지우거나, 보호 설정(DB 공개·백업 보존일·버킷 공개·버전 관리·암호화)을 약하게 바꾸지 않는다. 대상 finding 은 사라지고 재검사도 통과하는 패치라 8번(재검사)만으로는 못 막는다
7. 비밀처럼 보이는 env 를 새로 넣지 않는다
8. 적용한 명세를 다시 static_check 하면 대상 finding 이 사라지고 새 finding 이 생기지 않는다

8번 때문에 루프 안 재검사는 대부분 첫 회차에 끝난다. LOOP_EXHAUSTED 는 안전장치로 남아 있다.
LOOP_EXHAUSTED 는 "AI 가 또 고치려 한다" 는 뜻으로 두 경우에도 쓴다 — 봇 커밋을 다시 검토했는데 fix(`autofix_commit`),
사람이 고친 명세를 재검사했는데 fix(`human_decision`). 사유 코드를 늘리지 않으려고 같은 코드를 쓴다.
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
| 사유 코드 | 7개 | `PATCH_MISSING`·`GENERATED_SPEC_UNVERIFIED` 추가 | 고칠 수 있는 finding 인데 LLM 이 패치를 안 낸 경우 · intake 가 baseline 없이 만든 명세(`generated_spec`)라 findings 가 없어도 사람이 공개 범위·env·replicas 를 확인해야 하는 경우 (10/09 ingress 삭제 장애) |
| low finding | — | 사람 확인 조건에서 제외 (`verdict.EXCLUDE_LOW_FROM_HUMAN`) | 결정 #6. 단 LLM 출력이 틀리면(CITATION_INVALID) low 만 있어도 needs_human |
| low 만 있을 때 | LLM 호출 | **LLM 호출 안 함** | 매 배포 경고 설명에 비용을 쓰지 않는다. 설명은 규칙 제목 |
| 데이터 있는 SQLite + replicas 2 | needs_human (IRREVERSIBLE) | **fix (replicas → 1)** | replicas 를 줄이는 건 되돌릴 수 있다. 엔진 전환 패치는 데이터가 있으면 범위 밖으로 막힌다(`sqlite-with-data-llm-converts-engine` 케이스) |

## overlay 렌더러 (결정 #7: 검토 서비스 코드가 만든다)

- 입력 `deploy_spec` → 출력 `apps/<앱>/overlays/<환경>/{kustomization,ingress,pvc-*}.yaml`. 디스크에 쓰지 않고 dict 로 준다.
- base 의 Rollout 컨테이너 0번을 **통째로** 바꾸고(replicas·env·probe·resources·volumeMounts), Service targetPort 를 맞춘다.
- **이미지는 건드리지 않는다.** 태그는 CI 가 base kustomization 의 `newTag` 로 갱신하고, overlay 에 `images` 를 두면 그 갱신을 덮어 무시하게 된다.
  컨테이너를 바꿀 때도 base 의 `image`(CI 태그가 붙은 값)를 JSON Patch `copy` 로 옮긴다 — 안 그러면 태그 없는 이미지(`:latest`)가 된다.
- 지금 gitops 의 sample-app aws·gcp·local overlay 를 `kubectl kustomize` 결과 기준으로 그대로 재현한다(테스트). 차이는 의도한 한 가지 — `terminationGracePeriodSeconds` 명시.
- 시크릿은 `secretKeyRef`(`<앱>-secrets`)로만 렌더링한다. 값은 넣지 않는다.
- overlay 로 못 만드는 것(관리형 DB, 버킷, Secret 값 생성, 로컬·GCP 의 allowed_cidrs)은 `warnings` 로 돌려준다.
- 경고는 `RenderWarning`(str)이고 `code`·`blocking`·`doc_uri` 가 있다 (`overlay/warnings.py`). 커밋 단계는 문장을 파싱하지 말고 `rendered.blocking` 만 본다.
  `to_dict()` = `{code, blocking, message, doc}` — `doc` 은 멈춘 이유·고치는 법 문서(`warnings/<code>.md`).

  | code | blocking | 뜻 |
  |---|---|---|
  | `DB_PROVISIONING_REQUIRED` | ✅ | managed·in-cluster·external DB — overlay 로 안 만든다 |
  | `BUCKET_PROVISIONING_REQUIRED` | ✅ | 버킷은 인프라 쪽에서 만든다 |
  | `VOLUME_UNSUPPORTED` | ✅ | 대상 환경에 그 접근 모드의 스토리지가 없다 (지금 aws 는 볼륨 불가, STO-001) |
  | `TLS_HOST_MISSING` | ✅ | TLS 를 요구했는데 host 가 없어 인증서를 못 붙인다 |
  | `SECRET_KEYS_REQUIRED` | — | `<앱>-secrets` 에 키가 미리 있어야 한다. 없으면 Rollout 이 멈추고 자동 롤백 |
  | `TLS_SECRET_REQUIRED` | — | `<앱>-tls` 인증서 Secret 이 미리 있어야 한다 |
  | `INGRESS_CIDRS_NOT_ENFORCED` | ✅ | local·gcp 에서 allowed_cidrs 를 강제하지 못해 공개로 열린다 (aws 는 ALB 가 강제 — 경고 없음) |

```bash
.venv/bin/python scripts/render_overlay.py samples/01-pass-sample-app-aws.yaml   # blocking 경고가 있으면 종료 코드 3
```

## 근거 문서 (knowledge/)

S3 `review-docs` 버킷과 같은 구조다: `rules/{any,aws,gcp,local}/<ruleId>.md`, `incidents/K-*.md`(oneaction 리허설 카드 13장), `guides/*.md`, `warnings/<code>.md`(렌더러 경고 7개).
문서의 `## ` 섹션 하나가 청크다. 구현한 규칙 27개(P0 12 + P1 15) 전부 문서가 있고, 환경별 청크 수는 테스트로 30개 이상을 유지한다.

- 규칙 문서는 ruleId 정확 매칭(점수 1.0)으로 찾고, 사례·가이드는 의미 검색(dense cosine, 0.5 미만 버림)으로 보탠다.
- 정확 매칭은 ruleId 당 최대 8청크이고 **규칙 문서를 먼저** 채운 뒤 related_rules 사례·가이드를 붙인다(`retrieval.exact_first`). 경로 순으로 자르면 사례가 많은 DB-003 은 규칙 문서가 통째로 빠졌다.
- 임베딩은 로컬 `paraphrase-multilingual-MiniLM-L12-v2`(fastembed, 키 없음). 무관한 질문은 0.15~0.48 로 나와 임계값 0.5 아래다(검색 평가셋 참고).
- `warnings/` 는 렌더러 경고(`RenderWarning.code`)마다 멈춘 이유·환경별 차이·고치는 법이다. `rule_ids` 가 비어 있어 judge 프롬프트(규칙 정확 매칭·의미 검색)에는 들어가지 않는다.
  커밋 단계·보고서는 `warning.to_dict()["doc"]`(= `warnings/<code>.md`, 버킷에서도 같은 경로)로 찾는다. 문서의 `blocking` 은 테스트로 렌더러와 맞춘다.

```bash
docker compose up -d qdrant                                            # qdrant v1.19.1 (클라이언트와 같은 마이너)
.venv/bin/python scripts/index_knowledge.py --qdrant-url http://localhost:6333
```

다시 돌려도 같은 청크는 같은 point ID 라 중복되지 않고, knowledge/ 에 없는 point(지운 문서·줄어든 섹션)는 지운다(`--no-prune` 로 끔).

문서 원본은 이 레포의 `knowledge/` 이고, S3 버킷은 그 사본이다. 문서를 고친 뒤:

```bash
scripts/sync_knowledge_s3.sh oneaction-review-docs-<계정ID>            # dry-run
scripts/sync_knowledge_s3.sh oneaction-review-docs-<계정ID> --apply    # 업로드 (*.md 만, 지운 문서는 버킷에서도 지움) + 버킷 대조
.venv/bin/python scripts/index_knowledge.py --qdrant-url <주소>         # 색인 다시
.venv/bin/python scripts/check_knowledge_sync.py --bucket oneaction-review-docs-<계정ID> --qdrant-url <주소>
```

`check_knowledge_sync.py` 는 로컬 = S3 = Qdrant 를 개수만이 아니라 내용으로 대조한다 — 문서는 MD5 ↔ S3 ETag, 청크는 point ID ↔ 본문.
어긋나면 없는·남은·다른 항목 이름과 고치는 명령을 보여 주고 종료 코드 1. 하나만 볼 때는 `--bucket`·`--qdrant-url` 중 하나만 준다.

워커 쪽에서 버킷을 받아 색인할 때는 `aws s3 sync s3://<버킷>/ <dir>` 뒤 `index_knowledge.py --knowledge-dir <dir>`.

## 판단 사례 (review_cases)

끝난 검토에서 사람·AI 가 어떻게 판단했는지를 업무 DB `review_cases`(마이그레이션 0005)에 남기고, 다음 검토가 같은 규칙에 걸리면 judge 근거로 붙인다.
S3·Qdrant 가 아니라 업무 DB 인 이유: 근거 문서(FileRetriever)는 이미지에 구워져 있어 버킷에 올려도 재빌드 전엔 안 쓰이고, Qdrant 는 배포돼 있지 않다.

| 기록 시점 | 종료 방식(`outcome`) |
|---|---|
| 워커 `wait_human` 거절 | `rejected` (ops 없음) |
| 워커 `commit_fix` (PR 브랜치에 수정 커밋 → superseded) | 사람 응답이 없으면 `auto_fixed`, 있으면 아래 셋 중 하나 |
| 워커 `commit_overlay` 가 `committed` 이고 사람 응답이 있을 때 | `human_approved`(값 없이 그대로) · `human_edited`(사람이 직접 쓴 값 있음) · `recommended`(권장값만) |
| API `/webhooks/argocd` Degraded | `deploy_degraded`. 병합된 검토가 AI 수정 커밋 재검토(`requested_by=autofix:<원래>`)면 판단이 있는 원래 검토로 만든다 |

- **내용은 마스킹된 rounds·findings 에서만**: 규칙 ID·제목·경로·사람 확인 사유·적용한 값. finding `evidence`(값)는 넣지 않는다. ops 는 `/runtime/env`·`/secrets`·`/baseline`(과 그 위를 통째 교체하는 op)과 비밀처럼 보이는 값을 뺀다. 판단이 필요했던 finding(low 제외)이 없으면 남기지 않는다.
- `case_id = <review_id>.<outcome>` 이라 같은 종료 지점이 다시 돌거나 Argo CD 가 Degraded 를 여러 번 보내도 한 번만 남는다.
- **검색**(`retrieval.case_retriever`): ruleId 정확 매칭, 같은 대상 환경 먼저, 그 안에서 최근 3개. 문서는 `chunk_id=case:<case_id>`·`doc_type=case`·`rule_id`=걸린 규칙이라 인용 검증을 그대로 통과한다. 규칙 문서 뒤에 붙고, 저장소 오류면 경고 로그만 남기고 규칙 문서로 검토한다. 다만 근거 부족 게이트(`LOW_SCORE`)에서는 규칙 문서로 치지 않는다 — 문서 없는 규칙이 사례만으로 자동 판정되지 않게.
- judge 프롬프트(`judge-v3`)는 case 문서를 "지난 검토 기록, 규칙보다 우선하지 않고 참고로만"으로 다룬다.
- **권장값에는 쓰지 않는다.** 규칙·baseline 으로 권장값을 못 만드는 항목(빌드 플랫폼·시크릿 출처)은 관측 없이 정할 수 없는 값이라, 다른 검토에서 사람이 고른 값을 옮겨도 이 배포의 근거가 아니다.

## 평가셋 (eval/cases.yaml)

샘플 10개 + 제안서의 환각 케이스 12개 + P1 규칙 15개(p1-*) + 엣지케이스 10개(e01~e09). `reviewer` 자리에는 평가 대상 LLM 이, 나머지 자리에는 일부러 틀리는 가짜 LLM 이 들어가 코드 게이트가 잡는지 본다.

엣지케이스는 명세가 아니라 PR 상태에서 시작한다. 실행기(`evaluation.py`)가 Review API 와 같은 순서를 밟는다 — 원문 분류(`load_spec` 과 같은 기준) → 레포 분석(`eval/repos/sample-app.yaml`) → 생성(`prepare_intake`) 또는 LLM 복구(`repair_intake`) → 검토. 사람 응답 케이스는 승인 API 와 같은 검사(`resolve_human_decision` → `check_edited_ops`)를 거쳐 재개한다.

| id | 입력 | 기대 |
|---|---|---|
| e01·e02 | 파일 없음·주석뿐인 파일 | 레포 분석으로 생성 → pass, judge 호출 0 |
| e03 | 들여쓰기 한 칸 밀린 sample-app (YAML 오류) | 복구 → 샘플과 같은 명세 → pass |
| e04 | `replica` 오타 + `target` 누락 (스키마 오류) | 오타는 옮기고 target 은 분석값으로 복구 → pass |
| e05·e06 | 엔진 변경 needs_human 에 값 없이·engine 만 승인 | 권장값(postgres 16) 보충 → pass |
| e07 | 없는 경로(`/database/enigne`) 승인 | 승인 거절(API 422), 상태 그대로 |
| e08·e08b | 복구 LLM 이 env 를 지어내거나 포트를 바꿈 | 복구 게이트가 버림(INVENTED_VALUE·VALUE_CONFLICT), 검토 안 함 |
| e09 | 깨진 YAML 안 평문 토큰 | 복구·judge 프롬프트에 토큰 없음, 커밋본엔 원문 값 복원, SEC-001 needs_human |
| p1-db006·sec004·sto002·run005 | 관리형 DB 공개·env/secrets 중복·RWO 볼륨 + replicas 2·상한 없음 | 자동 수정 게이트 통과 → pass (SEC-004 는 겹친 env 삭제만) |
| p1-sto006 | baseline 의 persistent 볼륨을 뺌 | needs_human(IRREVERSIBLE), 값 없이 승인하면 baseline 볼륨 복원 → pass |
| p1-db004·low-only | 영속성 선언에 저장소 없음 · liveness·상한만 없음 | needs_human(AUTOFIX_FORBIDDEN) · LLM 없이 pass |
| p1-db007·sec003·net002·sto004 | 관리형 DB 백업 0일·시크릿 이름 중복·내부 전용에 0.0.0.0/0·버킷 암호화 꺼짐 | 자동 수정 게이트 통과 → pass (SEC-003·NET-002 는 해당 항목 삭제만) |
| p1-db008 | 데이터 있는 DB 의 메이저 버전을 낮춤 | needs_human(IRREVERSIBLE), 값 없이 승인하면 baseline 버전 복원 → pass |
| p1-sec002 | aws 에 gcp-secret-manager 참조 | needs_human(AUTOFIX_FORBIDDEN) |

복구 자리(`intake.repair`)는 `--llm fake` 면 정답(고치기 전 샘플)을 내는 가짜(`intake/fake_repair.py`), `--llm claude` 면 실제 Claude 다. 거절된 intake 의 verdict 는 `intake_rejected`(`/verify` 의 `intake_{status}` 와 같은 뜻).

```bash
.venv/bin/python scripts/run_eval.py                                  # 가짜 LLM, 키 없이
ANTHROPIC_API_KEY=... .venv/bin/python scripts/run_eval.py --llm claude --repeat 3   # judge·복구 모두 Claude
.venv/bin/python scripts/run_eval.py --only e                         # 엣지케이스만
.venv/bin/python scripts/run_eval.py --retriever qdrant               # 로컬 임베딩 + 메모리 Qdrant
```

지표: 기대 verdict 일치율 · 인용 유효율 · pass 기대 케이스 오탐 · LLM 호출 수(복구 포함)·토큰·시간. 결과는 `eval/reports/`(git 제외).

2026-10-09 실측(Claude Sonnet, `--only e`): 10/10 일치. 복구 3건(e03·e04·e09) 모두 값 보존·비밀 복원, 건당 5~9초. 전체 9회 호출 ≈ $0.14.

## 검색 평가셋 (eval/retrieval_cases.yaml)

"질문 → 나와야 할 문서" 25개 — 증상 질문 16 · 워커가 만드는 finding 검색어("제목 — 근거") 5 · 관련 문서가 없는 질문 4.
의미 검색(사례·가이드)만 잰다. 규칙 문서는 ruleId 정확 매칭이라 잴 필요가 없다.

```bash
.venv/bin/python scripts/eval_retrieval.py                              # 메모리 Qdrant 에 색인해서
.venv/bin/python scripts/eval_retrieval.py --qdrant-url http://localhost:6333
```

| 지표 | 뜻 |
|---|---|
| `hit@1`·`hit@3` | 임계값 없이 상위 1·3개 청크 안에 정답 문서 |
| `served@3` | 임계값(0.5)까지 적용해 워커가 실제로 받는 상위 3개 안에 정답 문서 |
| `negative_false_hits` | 관련 문서가 없는 질문에 임계값을 넘는 결과가 나온 수 |
| `misses_covered_by_rule` | 의미 검색은 놓쳤지만 정답 문서가 related_rules 정확 매칭으로 이미 들어가는 finding |

2026-10-08 실측(MiniLM, 131청크): hit@1·hit@3·served@3 = 20/21(0.952) · MRR 0.96 · 무관 질문 오탐 0.
놓친 1건은 SEC-001 finding(근거가 `***MASKED***` 라 뜻이 없음)인데, 정답 문서 둘 다 related_rules: [SEC-001] 로 정확 매칭돼 워커는 받는다.
여유가 작은 곳: RUN-001 finding 의 정답 점수 0.50, readiness 증상 0.53 — 임계값을 올리면 먼저 빠진다.

## 개발

```bash
cd ai && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # = pip install -e .[dev]
.venv/bin/pytest -q --cov=review_ai
.venv/bin/python scripts/validate_samples.py
```
