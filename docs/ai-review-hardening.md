# AI 검토 정확성·근거·제한된 자동 수정

## 변경 묶음

1. 평가·verdict: intake가 만든 미확인 경로를 운영과 동일하게 전달한다. 미확인 이미지가 남는 생성 사례를 추가했다.
   근거 충족 여부는 finding별 정식 규칙 문서로 확인한다. 사례·사고·다른 규칙의 높은 검색 점수는 대신할 수 없다.
2. 인용: `judge-v4`는 실제 `chunk_id`를 받는다. 자기 규칙을 인용해야 하며 인용 ID는 제공된 문서에 있어야 한다.
   공통 가이드의 대표 rule_id 때문에 규칙 ID와 조각의 ID 집합을 완전히 같게 요구하지 않는다.
   유효한 인용에는 비밀을 제거한 당시 본문 일부(800자), 출처, 본문 해시를 저장한다. 기존 이력은 규칙 ID로 계속 표시한다.
3. 결정적 수정: `build_recommendations`의 `source == "rule"` 후보만 재사용한다.
   새 수정값 계산 로직은 없고, 기존 `build_patch`의 범위·데이터 보존·비밀·재검사 게이트를 그대로 통과해야 한다.
4. 실패 재발 관찰: 기존 사례 조회 결과에서 운영자의 성공 배포 확인과 변경 조건이 있고 현재 값이 실패 조건에 해당하는
   사례를 집계한다. `decision.failure_case_observation`에 기록하며 verdict·사유 코드·승인 경로에는 반영하지 않는다.
   신규 DB 마이그레이션이나 배포 차단은 없다.

## 단계적 활성화

| 설정 | 기본값 | 동작 |
| --- | --- | --- |
| `REVIEW_STRICT_CITATIONS` | `false` | `true`이면 자기 규칙의 실제 규칙 문서 조각 인용이 필수다. |
| `REVIEW_DETERMINISTIC_FIXES` | `false` | `true`이면 허용 목록의 확정값으로 LLM 없이 수정할 수 있다. |

설정은 worker 시작 시 읽는다. 허용값은 true/false/1/0이다. 잘못된 값이면 시작을 거부한다.
기본 인용 모드에서도 지어낸 규칙·조각 ID나 중복 조각은 허용하지 않는다. 조각 누락은 `rule_only`로 기록하고 기존 규칙 ID
검증으로 처리한다. 엄격 모드에서는 조각 누락이 `CITATION_INVALID`다. 실제 Claude 평가 후 엄격 모드를 활성화한다.
설정을 false로 되돌리고 worker를 재시작하면 이후 judge 호출에 기본 정책이 적용된다. 이미 적용·커밋한 수정은 되돌리지 않는다.
평가 상태 보정과 규칙 근거 누락 수정은 정확성 수정이므로 기능 설정으로 끄지 않는다.

결정적 수정 허용 규칙은 `DB-006`, `STO-003`, `STO-004`다. 이 목록 밖의 중요 finding이나 자동 수정 금지 항목이 섞이면
결정적 경로를 쓰지 않는다. 생성 스펙 확인·봇 커밋 재수정·사람 편집 뒤 재수정·회차 제한을 우회하지 않는다.
설명은 규칙의 finding 제목과 기존 권장값 설명을 사용하고, 자기 규칙 문서 조각을 인용한다.
`llm_available=false`, `deterministic_available=true`, `patch_source=deterministic`으로 실제 출처를 구분한다.

`STO-003`의 비공개 전환은 이미 카탈로그가 허용한 정책이며 이번 변경이 만든 정책이 아니다.
의도적인 공개 정적 호스팅에는 영향을 줄 수 있으므로 이 기능을 활성화할 때 해당 앱의 공개 요구를 확인한다.
현 규칙은 공개 제공을 CDN·서명 URL 등으로 사람이 정하도록 요구한다.
버킷은 Kubernetes overlay로 생성하지 않으므로 명세 변경만으로 실제 버킷 설정이 바뀌었다고 판단하지 않는다.

실패 관찰은 같은 앱·레포·환경에서 최대 50건을 조회하며, 화면의 5개 표시 상한과 분리해 계산한다.
조회 상한에 도달하면 `search_complete=false`, 조회 실패이면 `status=unavailable`, `match_count=null`이다.
원인 가설이나 조건 없는 해결 기록을 검증된 재발로 세지 않는다. 인프라·클러스터 맥락과 실제 인과는 검증하지 않으므로
`enforcement_ready=false`를 명시한다. 향후 차단 정책은 사례 품질과 승인·복구 설계를 별도로 검토해야 한다.

## 검증

표준 개발 환경은 루트에서 `pip install -e './ai[dev]' -e ./common -e './api[dev]' -e './worker[dev]'`로 준비한다.

```sh
(cd ai && ../.venv/bin/python -m pytest -q --cov=review_ai --cov-fail-under=90)
(cd ai && ../.venv/bin/python scripts/validate_samples.py)
(cd ai && ../.venv/bin/python scripts/run_eval.py)
(cd ai && ../.venv/bin/python scripts/run_eval.py --strict-citations)
(cd api && ../.venv/bin/python -m pytest -q)
(cd worker && ../.venv/bin/python -m pytest -q)
```

Claude 키가 실행 환경에 설정된 경우 `scripts/run_eval.py --llm claude --strict-citations --repeat 1`로 실제 출력을 평가한다.
키를 커맨드 인자·로그·평가 보고서에 넣지 않는다. 평가용 명세와 규칙 문서만 사용하고 현재 운영 명세로 대체하지 않는다.
평가 통계는 최종 상태뿐 아니라 수정 회차의 검증 기록까지 읽는다. 위험한 오통과는 needs_human/intake_rejected 기대가
pass/fix로 나온 경우와 fix 기대가 pass로 나온 경우다. `chunk_citation_valid_rate`로 기본 모드에서도 조각 누락을 볼 수 있다.

통합 테스트는 별도 Postgres·Kafka를 사용한다. `REVIEW_IT_DSN`과 `KAFKA_BOOTSTRAP`을 지정하고 루트에서
`python -m pytest -q tests/integration`을 실행한다. 필수 서비스가 없어 건너뛴 테스트는 통과로 보고하지 않는다.

## 인계·배포

평가·verdict 변경을 먼저 검토하고, 인용 AI/API/UI 계약을 다음 묶음으로 검토한다. 결정적 수정과 관찰 연결은 그 뒤다.
API·worker 담당자는 추가 JSON 필드와 기존 이력 표시를 확인한다. 배포 담당자는 CI·이미지·실제 환경의 승인 및 재개를 확인한다.
실제 Claude 평가 통과와 로컬 통합 테스트는 운영 배포 완료를 뜻하지 않는다. 설정 기본값을 유지한 첫 배포 뒤,
대상 앱 요구와 결과를 확인해 각 기능을 활성화한다.

## 이번 작업 검증 결과 (2026-10-10)

작업 브랜치: `feat/ai-review-hardening`, 기준 main: `4a81e74`. 운영 배포·PR 생성은 이 검증에 포함하지 않는다.

| 검증 | 결과 |
| --- | --- |
| AI 전체 테스트 | 966 통과, 커버리지 97.59% (기준 90%) |
| API 전체 테스트 | 231 통과 |
| worker 전체 테스트 | 148 통과, 마지막 설정·수정 경로 변경 후 관련 9건 재검증 통과 |
| Postgres·Kafka 통합 테스트 | 27 통과, skip 0 |
| 가짜 LLM 평가 | 기본·엄격 모드 각각 57/57 통과 |
| 실제 Claude 모드 평가 | 최종 57/57 통과, 인용·문서 조각 유효율 100%, 위험한 오통과 0 |
| 샘플·규칙·명세 스키마 정합성 | 통과, 기존 DeploySpec JSON Schema 변경 없음 |
| UI | 두 화면의 JS 구문 검사 및 실제 브라우저에서 현재 인용·수정 이력 표시 확인 |

실제 Claude 모드의 reviewer/복구 자리는 프로젝트에서 사용 중인 Claude API로 실행했다.
고의로 잘못된 출력을 만드는 gate 사례는 설계대로 가짜 LLM을 사용하며, LLM이 필요 없는 사례는 호출하지 않는다.
키는 기존 AWS Secret에서 실행 프로세스 메모리로만 읽었고 로그·파일·보고서에 저장하지 않았다.

첫 전체 Claude 평가에서는 rollout이 없는 명세에 `/rollout/strategy`를 add한 출력 1건을 게이트가 거부했다 (56/57).
부모 객체가 실제로 존재해야 한다는 프롬프트 안내와 회귀 테스트를 추가했다. 해당 사례는 실제 Claude 3회 연속 통과,
이후 전체 재평가도 57/57 통과했다. 최초 실패 기록도 유지한다.
최종 로컬 보고서: `ai/eval/reports/20261010-191033-513858-claude.json` (git 제외).

API 첫 검사에서 npm 메타데이터 조회가 네트워크 제한으로 실패했으며, 네트워크가 허용된 환경에서 전체를 다시 실행해 통과했다.
통합 검증은 기존 로컬 5432 서비스 대신 별도 15433·19093 포트와 테스트 전용 DB를 사용했다.
이번 작업이 만든 컨테이너·네트워크·테스트 데이터 볼륨·미리보기 서버는 검증 뒤 정리했다.
