# 부산 자동 수정·도쿄 블루그린 로컬 검증

2026-10-10, 브랜치 `fix/local-demo-review`. 로컬 수정과 검증만 완료했다. 커밋·push·PR·클라우드 배포는 하지 않았다.

## 기준 코드와 변경

기준 HEAD는 `4a81e745521ff03d2e1bfe51d827be5093ca9614`다. `feat/ai-review-hardening` 작업트리의 미커밋 개선 파일 30개를 복사해 독립된 작업트리에서 검증했다. 원본 작업트리와 인덱스는 변경하지 않았다. 복사 당시 파일 해시는 git에서 제외되는 `ai/eval/reports/local-demo-inherited-snapshot.json`에 기록했다. 기존 개선 작업과 이번 수정이 모두 미커밋 상태이므로 이후 통합 시 변경 출처를 구분해야 한다.

- `RUN-005`는 local에서만 medium으로 판정한다. AWS·GCP는 기존 low 경고 정책이다. 이는 팀 합의 전의 **로컬 정책 후보**이며 전체 local 앱에 적용된다.
- 부산 누락 상한은 기존 LLM 경로로 고친다. `REVIEW_DETERMINISTIC_FIXES` 허용 목록에 RUN-005를 추가하지 않았다. 제한을 결정적으로 채우는 성능 개선과 시연 정책을 섞지 않는다.
- 리소스 패치는 요청값과 기존 상한을 보존하고, 누락된 상한만 채워야 한다. 요청값보다 작은 상한은 거부한다. 기본값은 250m / 128Mi이며, 이보다 큰 요청이 있으면 사람이 상한을 정한다.
- 리뷰에서 발견한 승인 우회도 수정했다. AI 패치·권장값 생성·승인 API·워커 재개·원본 커밋 직전에 같은 수량 비교 검사를 사용한다. 잘못된 기본값은 제안하지 않고, 저장된 권장값과 직접 입력도 검사한다. 안전한 기본 상한이 없는 상태에서 일반 승인은 422로 거부한다. `use_recommendations:false`로 상한 생략을 명시 승인하는 기존 계약은 유지하지만, 요청량보다 작은 상한을 선언한 명세는 이 선택으로도 승인할 수 없다.
- `rollout.auto_promotion: false`가 최종 Rollout의 `autoPromotionEnabled: false`로 렌더링된다. 필드를 생략한 기존 명세는 자동 승격을 유지한다.
- 블루그린 패치가 `/rollout` 부모 없이 하위 경로를 추가하면 계속 거부한다. 기존 개선의 프롬프트 수정과 함께, rollout 생략·빈 객체·strategy 생략·기존 수동 승격 설정에 대한 회귀 테스트를 추가했다. 가짜 LLM도 기존 객체의 strategy가 생략된 경우를 처리하도록 수정했다.

## 시연 입력의 계약

| 입력 | 기대 흐름 | 범위 |
|---|---|---|
| `ai/samples/23-fix-busan-resource-limits.yaml` | RUN-005 → fix 이력 → 수정 커밋 → 새 SHA 재검토 pass → verify 성공 → CI 대기 | 실제 DB·Kafka, GitHub는 가짜 |
| `ai/samples/24-human-tokyo-manual-bluegreen.yaml` | breaking 선언 → needs_human → 승인 API → 재시작한 워커 재개 → verify 성공 → 수동 승격 설정 렌더링 | DB 연결·마이그레이션·클러스터 승격은 미검증 |

도쿄에서 승인 한 번으로 진행하려면 **처음부터 bluegreen과 auto_promotion:false를 선언**한다. canary 원안의 권장값을 승인해 bluegreen으로 수정하면, 재검사에서 RUN-006 breaking 사유가 남아 다시 사람 확인으로 돌아간다. 이 흐름을 검증했으며 자동 승인으로 우회하지 않았다. 변경 커밋 이후 기존 승인 재사용 문제는 별도 SHA·승인 계약이 필요하다.

블루그린은 breaking 마이그레이션 자체의 안전성을 보장하지 않는다. 현재 PreSync 마이그레이션 이후 승격 전까지 옛 코드가 새 스키마를 읽을 수 있으므로, 실제 SQL·앱 호환성은 별도 리허설에서 확인해야 한다. 도쿄 입력의 외부 DB와 Secret도 실제 준비 여부를 확인해야 한다.

## 검증 결과

아래 표는 최초 구현 검증 결과다. 이후 승인 우회 수정의 재검증 결과는 다음과 같다.

- AI 전체 1,028건, Worker 전체 161건, API 전체 237건 통과.
- 실제 PostgreSQL·Kafka 통합 테스트 전체 39건 통과(skip 없음, 59.75초). 이 중 시연 흐름 12건은 기존 부산·도쿄와 큰 요청량의 부산 승인 흐름에 deterministic/strict 옵션 네 조합을 적용했다. 큰 요청량은 일반 승인·잘못된 상한 입력 모두 422, 안전한 상한 입력 후 재시작한 워커의 승인 재개·새 SHA 재검토·CI 대기를 확인했다.
- 총 1,465건 통과. strict fake 평가셋도 59/59, unsafe false pass 0이며 보고서는 `ai/eval/reports/20261010-194929-650005-fake.json`이다. `git diff --check` 통과.
- 요청량이 큰 부산 입력에서 잘못된 기본값·직접 입력을 차단하고 안전한 값 입력 후 수정 커밋·새 SHA 재검토로 진행하는 회귀 테스트를 추가했다. CPU의 소수/밀리 단위, 메모리 Mi/Gi/Ti 단위, 동등한 수량, 기존 저장 권장값, 요청량 수정, 상한 생략의 명시 승인도 검사한다.

| 검증 | 결과 |
|---|---|
| AI 전체 단위·렌더링 테스트 | 1,016 passed, 10.91초 |
| Worker 전체 테스트 | 157 passed, 4.51초 |
| API 전체 테스트 | 231 passed, 8.60초 |
| 실제 PostgreSQL·Kafka 통합 테스트 전체 | 35 passed, 46.94초; skip 없음 |
| 위 통합 테스트 중 부산·도쿄 신규 흐름 | 8 passed: deterministic / strict 옵션 네 조합 × 두 환경 |
| fake 평가셋, 호환 모드 | 59/59, unsafe false pass 0 |
| fake 평가셋, strict 모드 | 59/59, unsafe false pass 0 |
| 최종 Kustomize 출력 | 수동 승격 false, 자동 승격 시간 없음 확인 |

통합 테스트는 API를 ASGI로 호출하고, 실제 PostgreSQL 스키마·체크포인트·Kafka 발행/소비를 사용한다. GitHub·Claude는 가짜다. CI 이후 병합 호출은 가짜 GitHub로 확인하고, GitOps 커밋 단계는 로컬 렌더링 뒤 `LOCAL_RENDER_ONLY`로 blocked 종료한다. 이 blocked는 PR B의 ingress 보호 판정과 다르다.

API 첫 실행은 샌드박스의 npm 네트워크 제한으로 lockfile 테스트 1건이 실패했다. 패키지 메타데이터 조회가 가능한 환경에서 전체를 다시 실행해 231건 통과했다. 테스트 목적의 임시 package-lock만 생성하며 저장소 파일은 변경하지 않는다.

기존 개선 작업의 Claude 보고서 `20261010-191033-513858-claude.json`는 수정된 프롬프트로 전체 57/57 통과, unsafe false pass 0을 기록한다. 그 이전 블루그린 대상 3회 반복도 통과했다. **이 브랜치에서 Claude API를 새로 호출하지 않았으므로, 추가한 부산 정책·도쿄 입력을 실제 Claude로 검증 완료했다고 볼 수 없다.**

## 로컬 재실행

Python 3.13 이상과 프로젝트 의존성(ai, common, api, worker 및 각 테스트 의존성)이 설치된 환경에서 실행한다. 각 모듈의 테스트는 해당 디렉터리에서 `python -m pytest -q`로 실행한다. 루트 pytest는 통합 테스트만 실행한다.

```sh
docker compose -p crystal-demo-review-local -f tests/compose.demo.yaml up -d --pull never --wait
REVIEW_IT_DSN=postgresql://review:review@127.0.0.1:15443/oneaction_review \
KAFKA_BOOTSTRAP=127.0.0.1:19193 \
PYTHONPATH="$PWD/ai:$PWD/api:$PWD/worker:$PWD/common" \
python -m pytest -q tests/integration
docker compose -p crystal-demo-review-local -f tests/compose.demo.yaml down
```

통합 테스트는 별도 테스트 DB를 생성·삭제한다. 전용 컨테이너는 종료했으며 프로젝트 볼륨은 보존했다. 기존 Docker 프로젝트와 클러스터는 변경하지 않았다. fake 평가는 ai 디렉터리에서 `python scripts/run_eval.py` 및 `python scripts/run_eval.py --strict-citations`로 실행한다.

## 담당자와 맞출 항목

1. 부산 정책 후보 수용 여부. 환경별 규칙 심각도가 달라지는 정책이며 실제 적용 전 합의한다.
2. 부산 봇 커밋으로 HEAD가 바뀐 뒤 세 환경 검토·승인 집계. 현재 검증은 환경별 독립 요청이며 동일 PR의 세 환경 집계 성공을 증명하지 않는다.
3. 도쿄 수동 승격 화면과 GitOps 최종 Kustomize 출력. 새 auto_promotion 필드는 구버전 API의 extra=forbid에 거절되므로 새 서비스 버전 준비 후 명세에 사용한다.
4. 개발자 진행 화면의 수정 커밋·에러율·중단 사유, 플랫폼 타임라인, 서비스 3분할 카운터. 오류 주입 변수는 sample-app의 실제 이름인 FAIL_RATE를 사용한다.
5. 실제 Claude 재검증 후 두 PR·세 클러스터 전체 리허설. 4분 완료·20초 중단·최대 피해 트래픽 수치는 실측 전까지 확정하지 않는다.
