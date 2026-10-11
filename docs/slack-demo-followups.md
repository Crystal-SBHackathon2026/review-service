# 10월 10일 19:30 이후 Slack 추가 요청 처리

## 통합 PR 작업과의 경계

다른 채팅 **세 브랜치 병합 가능성 점검**의 최신 통합 후보 `f679ef9`를 기반으로 한다.
kubectl 보안 CI 수정(`8e26de7`)과 동료 PR #65의 실패 이벤트 연결·자동 중단·부분 완료·Draft 보호를 가져왔다.
이 문서의 추가 diff에는 해당 변경을 다시 구현하지 않았다. 진행 화면 파일은 양쪽에서 사용하지만,
이번 추가 기능은 **실패 사례 저장 여부 표시**다. 실패 이벤트 연결·중단 판정은 #65 구현을 사용한다.

| Slack 요청 | 처리 결과 | 기존 통합 PR과의 관계 |
| --- | --- | --- |
| GCP 공개 접속을 renderer 재생성 후에도 유지 | GCP 전용 `network.service` 명세, active Service LoadBalancer patch, 삭제 보호 추가 | 다중 환경 renderer의 추가 계약. CI 수정과 별개 |
| 장면 15에서 저장된 실패 사례 확인 | 공개 진행 화면에 저장 여부·분석 상태·운영자 상세 링크 추가 | 기존 저장/분석 기능을 사용. 상세 진단 공개 및 401 해제는 하지 않음 |
| PR A 사전 준비 | 별도 sample-app 체크아웃의 로컬 `demo/pr-a-main` 브랜치에 입력 준비 | 기존 부산 RUN-005 자동 수정 정책 사용. 새 정적 차단 규칙 추가 없음 |
| 규칙 개수·FAIL_RATE 차단 정책 | 31개 확인. FAIL_RATE 차단 규칙 추가 없음 | 기존 통합 후보 유지 |
| 실제 실패 분석 소비·local 알림·WIF 갱신·외부 접속 | 실제 리허설 확인 필요 | 공동 인프라 확인 항목. 로컬 테스트를 실서비스 성공으로 계산하지 않음 |

요청 근거: [19:32 장면·실패 사례 요청](https://softbankhackathon2026.slack.com/archives/C0C1V166M5L/p1791628341736839),
[20:01 PR A 준비 요청](https://softbankhackathon2026.slack.com/archives/C0C1V166M5L/p1791630110514099),
[20:56 GCP 외부 접속 계약 요청](https://softbankhackathon2026.slack.com/archives/C0C1V166M5L/p1791633368383139).

## GCP 외부 접속 계약

```yaml
network:
  service:
    type: LoadBalancer
    public: true
rollout:
  strategy: bluegreen
  auto_promotion: false
```

`service-public.yaml`은 기존 active Service를 patch하므로 같은 이름·selector를 유지한다.
preview는 ClusterIP다. Ingress와 동시 지정, AWS/local 지정, NodePort, public false는 명세 단계에서 거절한다.
HTTP 공개 진입점은 기존 NET-001 low 경고다. TLS 종료가 구현된 것으로 표시하지 않는다.
현재 공개 Service patch를 명세에서 빼면 단일·다중 환경 모두 병합/커밋 전에 보호 리소스 삭제로 차단한다.
실제 GCP 방화벽, IP 할당, 브라우저 HTTP 직접 접속 가능 여부는 박찬건·김혜연과 확인해야 한다.
NET-001 근거 문서도 공개 Service 조건과 맞췄다. 머지 후 S3·Qdrant 지식 동기화와 내용 일치 검사가 필요하다.

## 실패 사례 화면

검토에 연결된 같은 앱·저장소·대상 환경의 최근 관찰 최대 10건에서 실패만 표시한다.
저장된 사례가 실제 존재하는지 확인하고 저장 여부와 분석 대기/처리/완료 상태를 별도로 표시한다.
명세 원문·진단·로그·해결 내용은 공개 진행 응답에 추가하지 않는다.
상세 링크 `/ui#deploy:<event_id>`는 기존 운영자 UI이며 토큰이 필요하다.
`case-advice`의 401은 인증 계약이다. 본인 SSO로 받은 운영자 토큰을 UI에 입력해야 한다.
저장은 다음 검토에서 조회할 사례를 남긴다는 뜻이며 모델 가중치 학습이나 분석 완료를 뜻하지 않는다.

## PR A 입력과 리허설 조건

별도 `slack-followups/sample-app` 체크아웃의 `demo/pr-a-main`은 main `f9fe903`에서 시작했다.
UI 문구 한 줄(`Crystal Sample App · v2`)과 다음 명세를 준비했다. 원격 푸시·PR 생성·배포는 수행하지 않았다.

| 입력 | 목적 |
| --- | --- |
| 루트 deploy.yaml 및 deploy/aws.yaml | 기존 AWS canary, FAIL_RATE 문자열 0.3 |
| deploy/local.yaml | CPU/memory limit 누락 → 기존 RUN-005 medium 자동 수정 |
| deploy/gcp.yaml | DB 변경 없이 공개 active Service, bluegreen 수동 승격 |
| deployment-request.yaml | 등록된 AWS/local/GCP 선택 목록 |

이 준비물은 **통합 다중 환경 리허설용**이다. AWS의 limit 누락은 기존 low이므로 AWS만으로는 같은 자동 수정 장면이 나오지 않는다.
API/worker의 MULTI_TARGET_ENABLED와 sample-app repository variable, 등록 대상 목록을 담당자가 맞춘 뒤 사용한다.
새 명세 스키마·renderer 배포가 먼저다. 기존 코드에 GCP service 명세를 먼저 보내면 schema_error가 난다.
기본 비활성 상태로 루트 명세만 검토하면 부산 자동 수정 장면은 실행되지 않는다.
외부 PostgreSQL은 DB_PROVISIONING_REQUIRED가 남아 있어 이 입력에서 제외했다.
GCP DB 변경·사람 승인 장면을 포함하려면 검증된 DB 계약을 별도로 준비해야 한다.

PR A 로컬 검증은 fake LLM/GitHub와 실제 그래프·조정자·Kustomize를 사용한다.
부산 명세만 수정 커밋 → 세 환경 새 SHA 재검토 → 모두 pass → 단일 머지·GitOps 커밋 순서를 확인했다.
실제 Claude 응답·GitHub CI·Argo 배포 성공을 이 결과가 증명하지는 않는다.

## 대본 및 노트북 ② 운영 메모

- 장면 3: 정적 규칙은 31개. 공식 문서와 과거 실패 사례를 조회해 검토한다. 근거 없는 의견을 확정 설명으로 읽지 않는다.
- 장면 4: PR C #31의 보호 ingress 제거 차단, PR B #33의 DB-004 needs_human 거절을 별도 예로 설명한다.
- 장면 5: 부산 limit 누락의 수정 커밋과 **새 SHA** 재검토를 보여준다. FAIL_RATE는 분석 단계의 장애 주입이다.
- 장면 6: 도쿄 수동 bluegreen 승격과 AI 사람 승인은 서로 다른 단계다. DB 변경 승인 장면은 준비된 계약 없이 성공으로 설명하지 않는다.
- 장면 15: 실패 관찰 → 분석 요청 → 분석 소비 상태 → 실제 사례 저장을 분리해 확인한다. 화면 표시만으로 Kafka 소비 완료를 주장하지 않는다.

노트북 ②에서 `/ui`를 열고 운영자 토큰을 입력한다. PR B 해당 검토의 현재 SHA와 DB-004 판정을 확인한 뒤
리허설 진행 신호에 맞춰 거절한다. PR A 자동 수정으로 SHA가 바뀌었다면 이전 SHA의 사람 승인을 재사용하지 않는다.

local 알림은 20:43의 최신 API Gateway 수신 URL과 자체 SSO 토큰 계약을 사용해야 한다.
old platform ALB 주소를 그대로 복사하지 않는다. 송신 laptop 설정과 중앙 실제 수신을 함께 확인한다.
EKS `oneaction`을 임시 kubeconfig로 읽으려 했으나 API 연결 timeout이었다. live Kafka 소비·사례 행은 확인하지 못했다.
실제 재시도에서는 본인에게 연결된 EKS context를 확인한 뒤 관찰 시각·job 상태·case 존재를 읽고,
서비스 재배포 없이 알림 수신부터 검증한다.

21:00 성진님 보고에서 GCP 수동 승격은 `rollouts/status` RBAC 권한 부족으로 차단됐다.
찬건님 권한 반영 후 성진님이 실제 Promote와 같은 주소의 버전 전환을 확인해야 한다.
이 renderer 변경은 해당 권한을 수정하지 않으며 수동 승격 설정을 유지한다.

## 최종 검증

- AI 1,044개 통과. 스키마 생성·31개 규칙·12개 샘플·형식 거절 5개 확인.
- 최신 통합 후보를 반영한 API 246개, worker 204개 통과.
- disposable MULTITARGET_TEST_DSN이 없어 worker PostgreSQL 검사 4개는 건너뛰었다. DB 마이그레이션 변경 없음.
- 실제 PR A 입력을 사용한 오프라인 그래프/조정자 검증 1개, sample-app Node 테스트 8개 통과.
- Headless Chrome: 사례 저장·빈 이력·상세 API 401·390/1100px 화면·JS 오류 없음 확인.
- npm 레지스트리 조회와 임시 HTTP 포트 테스트는 sandbox 제한으로 실패한 뒤 허용된 환경에서 재검증해 통과했다.

PR A 입력·별도 오프라인 검증·화면 검증 캡처는 로컬 `slack-followups` 폴더에 보관했다.
PR A 파일은 이 개발 PR에 포함하지 않는다. 위 입력 계약을 바탕으로 리허설 때 sample-app에 올린다.
통합 원본 체크아웃과 기존 원격 PR에는 이 추가 diff를 적용하지 않고, #64 뒤의 별도 Draft PR로 검토한다.
