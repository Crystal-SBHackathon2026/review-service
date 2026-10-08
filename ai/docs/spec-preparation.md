# 빈 배포 명세 생성·검토

명세가 없으면 `review_ai.preparation`이 권장값으로 명세를 만들고, 기존 정적 검사 → 근거 검색(RAG) → judge → 허용된 자동 수정·재검사를 거쳐 최종 명세를 반환한다. 모든 생성에 사람 승인이나 입력 화면을 추가하지 않는다. 파이프라인이 이미 아는 설정을 `GenerationContext`로 전달하면 자동으로 검토를 통과할 수 있다.

## 소유권과 연결 범위

| AI 모듈 (이번 구현) | 파이프라인 (연결 담당) |
|---|---|
| 빈 입력 판별, 권장 프리셋·대상 능력표 적용 | 특정 커밋의 앱 레포에서 파일 조회 |
| 이전 승인 명세 보존, 확인된 설정 우선 적용 | 업무 DB에서 baseline·앱 설정 조회 |
| 기존 정적 검사·RAG·LLM 교정 재사용 | 레포 분석·이미지 빌드 관측값을 context에 전달 |
| 최종 명세·YAML·추천 이유·검토 결과·확인 항목 반환 | 생성 명세 저장·커밋, 새 SHA로 검토 요청·CI·배포 진행 |

현재 `/reviews`는 파일 없음 404, 빈 파일 422를 유지한다. 이 변경은 AI 라이브러리와 미리보기 함수이며 API·워커·Kafka·업무 DB·기존 verdict/State 형식은 바꾸지 않는다. 서버 진입점에 자동 생성이 이미 연결된 것으로 해석하지 않는다.

## 입력

`prepare_spec(raw, context=, baseline=None)`은 동기 생성/형식 검사만 한다. `prepare_and_review`는 같은 입력에 기존 그래프를 연결한다.

- `raw`: `None`, 빈 문자열·공백·주석뿐인 YAML, 빈 문서, YAML `null`, `{}`이면 생성. 비어 있지 않은 잘못된 명세는 형식 오류로 반환하며 자동으로 덮지 않는다. 배열·숫자·bool도 오류다.
- `context`: `GenerationContext`. `repository`, `target.env`, `target.region`은 필수이며 이미 선택된 앱·대상을 사용한다. 선택 필드 `name`, `image`, `runtime`, `requirements`, `database`, `secrets`, `network`, `storage`는 파이프라인이 확인한 설정이다. 이 함수는 레포를 직접 읽지 않는다.
- `baseline`: 같은 레포·앱·환경의 업무 DB 승인 명세. 사용자가 보낸 YAML 안에는 넣지 않는다. 생성 시 승인된 DB·영속 볼륨·시크릿·공개 설정 등을 보존한다. context가 지정한 변경도 기존 baseline 비교 정적 검사를 통과해야 한다.

생성 우선순위는 **context → 같은 앱의 승인 baseline → 서비스 프리셋**이다. 유효한 기존 명세는 생성 프리셋으로 바꾸지 않는다.

## 프리셋 `container-http/v1`

| 설정 | 후보값과 근거 |
|---|---|
| 이름 | 레포 이름에서 DNS label 생성. 변환·축약 시 해시 접미사 |
| 이미지 | `ghcr.io/<owner>/<repo>`와 대상 능력표의 플랫폼. 실제 빌드 확인 필요 |
| 실행 | port 8080, replicas 1, CPU request 50m, memory request 64Mi, termination grace 30초 |
| readiness/liveness | 경로를 만들어 내지 않음. context 또는 baseline에서 사용 |
| DB·영속성 | 미선언 후보는 engine none, persistence false. 필요 없음이 확인됐다는 뜻은 아님 |
| 시크릿·볼륨·버킷 | 빈 목록 후보. 값·리소스를 임의 생성하지 않음 |
| 외부 접근 | 새 앱은 ingress 없음(클러스터 내부). 확인된 network를 받으면 사용 |

resources 기본값은 모델에 정의된 시작값이며 부하 실측치가 아니다. 태그·digest는 기존 CI가 결정한다. 모든 값의 적정성을 RAG만으로 보장하지 않는다. 포트·probe 경로·DB 사용 여부 등은 코드·빌드·실행 관측으로 확인해야 한다.

## 반환값

`PreparedReview`는 내부 원본과 검토 결과를 갖는다. `to_response()`는 JSON 직렬화 가능한 외부 응답이며 비밀값을 가린다.

| 필드 | 의미 |
|---|---|
| `origin` | `provided` 또는 `generated` |
| `preset` | 생성 정책 버전, 기존 명세는 null |
| `deploy_spec`, `yaml` | 교정까지 적용된 최종 명세. 외부 응답은 비밀 마스킹 |
| `recommendations` | 기본값·능력표·baseline을 적용한 경로·이유 |
| `verification` | 미확인 후보값. 사람이 답해야 한다는 뜻이 아니라 자동 분석/관측 작업 목록 |
| `review` | 기존 그래프의 status·decision·findings·rounds·applied_ops·근거 |
| `render_warnings` | 기존 overlay 렌더러의 경고와 blocking 여부 |
| `ready_to_commit` | 리뷰 pass + verification 없음 + 렌더러 blocking 없음 |

`review.status=pass`여도 미확인 요구사항이 남으면 `ready_to_commit=false`다. 확인 항목은 API에서 오류로 버리지 않고 후보 명세와 함께 반환한다. 확인된 context로 다시 호출하면 해결된다. `ready_to_commit`은 이 모듈의 게이트만 통과했다는 의미이며 이미지 CI 성공·GitOps base/Application 존재·Secret 존재·배포 성공을 보장하지 않는다.

기존 정책대로 finding이 없으면 검색·LLM을 생략하고, low 경고만이면 근거를 검색하되 LLM은 생략한다. 자동 교정 가능한 문제가 있으면 RAG 근거와 패치 게이트를 그대로 사용한다. DB 엔진 변경·근거 부족·실행 코드 수정 등 기존 사람 확인 조건은 그대로 유지한다. 일시 오류는 파이프라인 RetryPolicy를 위해 예외로 올린다.

## 파이프라인 사용 예

```python
from review_ai.preparation import GenerationContext, prepare_and_review

# file_missing은 레포·커밋 접근 성공 후 파일 부재를 확인한 경우만.
# 권한·타임아웃·GitHub 오류를 None으로 바꾸지 않는다.
raw = None if file_missing else file_content
result = await prepare_and_review(
    raw,
    context=GenerationContext.model_validate(observed_app_settings),
    baseline=approved_baseline,
    review_id=review_id,
    llm=deps.llm,
    retriever=deps.retriever,
)
response = result.to_response()
```

앱 설정이 충분하면 확인 화면 없이 진행할 수 있다. 기본 context만 있으면 후보를 반환하고 `verification`을 자동 관측 작업으로 처리한다. 해결할 수 없는 항목만 기존 사람 확인 흐름으로 보낸다.

현재 원본 조회 계약을 유지하려면 생성 명세를 앱 레포에 먼저 저장하고, **생성 커밋의 새 SHA**를 `spec_ref.commit`으로 하여 기존 리뷰 요청을 발행한다. 내용이 이미 바뀌었으므로 이전 커밋의 통과 결과는 재사용하지 않는다. 생성은 기존 autofix_commit 교정 루프와 구분한다. 명세 생성 커밋이라는 이유만으로 autofix_commit=True를 쓰지 않는다.

생성 명세 커밋은 워커 `commit_fix` 와 같은 `GitHubClient.prepare_file_commit`(Git Data API 트리)로 할 수 있다 — 파일이 없어도 blob SHA 없이 새 파일을 만든다. PR head가 바뀌었을 때 충돌 처리·CI 대기·저장·이벤트 발행도 파이프라인 책임이다. 생성 후보가 있는 동안 기존 `/reviews`에 없는 경로의 `spec_ref`만 보내면 계속 404다.

외부 응답의 가린 YAML을 원본에 덮어쓰지 않는다. 같은 프로세스의 저장 단계는 `result.prepared.spec.model_dump(mode="json", exclude_none=True)`를 사용하거나, 기존 원본에 `review.applied_ops`를 적용한다. 평문 비밀이 발견되면 기존 정적 검사가 막으므로 ready_to_commit은 false다. baseline은 앱 파일에 저장하지 않는다.

## 로컬 미리보기

```bash
cd ai
.venv/bin/python scripts/prepare_spec.py --context samples/generation-context-sample-app-aws.yaml
# 빈 파일을 전달하는 예: --spec /path/to/empty-deploy.yaml
# 실제 Claude 교정·설명이 필요하면 --claude (ANTHROPIC_API_KEY 필요)
```

sample-app 예시는 실제 앱 요구사항을 전달하므로 명세가 없어도 LLM/사람 확인 없이 통과한다. 임의 앱에 이 예시의 DB none·영속성 false를 복사하지 않는다. 명세 생성·보존·마스킹·baseline 데이터 보호·RAG 자동 교정·미확인 후보 게이트는 `tests/test_preparation.py`로 검증한다.
