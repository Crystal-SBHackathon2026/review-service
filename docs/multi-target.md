# 선택 환경별 검토와 배포 — 로컬 구현, 기본 비활성화

이 브랜치는 기존 AWS 단일 명세 경로를 유지하면서, 등록된 AWS/GCP/local 환경을 한 PR에서 검토·배포하는 경로를 추가한다.
새 클러스터 생성, 임의 고객 온프레미스 등록, 환경 전체의 자동 롤백은 범위 밖이다.
다른 AI 작업 폴더의 미커밋 변경을 포함하지 않은 별도 체크아웃에서 구현했다. 원격 푸시·PR·배포는 수행하지 않았다.

## 계약

앱 PR의 `deployment-request.yaml`이 선택 목록의 정본이다.

```yaml
apiVersion: oneaction/v1
kind: DeploymentRequest
targets:
  - env: gcp
    region: asia-northeast1
    cluster: tokyo-gke
    path: deploy/gcp.yaml
```

각 경로에는 기존 `AppSpec` 명세 하나가 있다. target.env/region은 선택 목록과 같아야 한다.
DB·runtime·namespace·storage 등 기존 설정을 환경별로 쓴다. 같은 요청의 모든 명세는 같은 앱·저장소·이미지 repository를 사용한다.
명세 누락/형식 오류는 명시적으로 거절한다. 기존 intake의 AWS 기본 대상 또는 다른 환경 baseline을 사용하지 않는다.
검토 baseline은 기존 워커의 `(app, env)` 조회를 재사용한다.

인증된 `POST /deployment-requests`:

```json
{
  "spec_ref": {
    "repository": "Crystal-SBHackathon2026/sample-app",
    "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "path": "deployment-request.yaml"
  },
  "pr_number": 7,
  "requested_by": "user"
}
```

선택 파일 경로는 CI와의 계약에 따라 루트 `deployment-request.yaml`로 고정한다. 현재 열린 동일 저장소 PR의 head만 허용하며 fork는 거절한다. 이미지 발행 CI가 실행되는 `main` 대상 PR만 허용하고, 머지 직전 CI 재확인 뒤 대상 브랜치를 다시 검사한다. PR 웹훅도 루트 선택 파일을 발견하면 같은 경로를 사용한다.
다중 환경 요청은 선택 파일과 환경별 명세로 처리한다. main 이미지 CI는 선택 파일을 먼저 확인하므로 기존 루트 `deploy.yaml`이 없거나 유효하지 않아도 그 파일을 읽지 않는다.
Draft는 환경별 검토는 준비하지만 조정자가 수정 커밋·머지를 보류한다.
`GET /deployment-requests/{id}`는 인증 후 부모 진행 상태, 환경별 검토 ID·상태와 실제 배포 결과를 반환한다.
각 환경의 사람 확인은 기존 `POST /reviews/{review_id}/decision`을 사용한다. 이전 SHA의 승인은 새 SHA로 이전되지 않는다.

## 흐름과 복구

1. 같은 PR head의 부모 요청 하나, 선택 환경별 자식 검토를 저장하고 발행한다. DB 멱등키가 중복 웹훅을 묶는다.
2. 자식은 검토·사람 승인만 처리한다. 개별 수정 커밋·CI 확인·머지·GitOps 쓰기는 하지 않는다.
3. 모두 준비되면 조정자가 JSON Patch를 각 원본 명세에 적용해 하나의 수정 커밋을 만든다. 원본 비밀값은 Kafka의 가린 사본으로 대체하지 않는다.
4. 새 SHA를 모든 선택 환경이 다시 검토한다. 자동 수정 커밋은 최대 2회이며, 새 검토가 추가 수정을 요구하면 기존 LOOP_EXHAUSTED 사람 확인 정책을 따른다.
5. 최종 PR SHA의 CI와 전체 overlay의 실제 `kubectl kustomize` 검사·보호 리소스 검사가 통과해야 머지한다. 공유 verify 상태는 부모만 쓴다.
6. main CI가 병합 SHA 이미지 빌드·푸시까지 성공하면, 선택 환경의 이미지 버전과 설정을 하나의 GitOps 커밋으로 갱신한다.
7. GitOps 커밋은 배포 성공을 뜻하지 않는다. 환경별 실제 알림으로 성공·실패·미확인을 집계한다.

조정자는 PR 키의 PostgreSQL session advisory lock으로 직렬화한다. 외부 쓰기 전의 fixing/merging 상태와 pending/merge SHA를 DB에 기록한다.
재시작은 PR 상태를 읽어 이미 한 머지를 반복하지 않는다. 발행이 누락된 received 자식은 기존 review recovery sweep이 재발행한다.
수정 커밋의 ref 갱신이 일시적으로 실패하면 후속 요청을 유지한다. 후속 요청을 먼저 조회한 worker도 ref 갱신을 기다리며, 실제 PR head 변경·종료가 확인된 경우에 이전 요청을 폐기한다. Draft로 바뀐 요청은 ref 갱신도 보류한다.
GitOps ref 충돌은 새 snapshot을 읽어 검사부터 다시 한다. 검토 이후 선택 overlay/base가 바뀌면 덮어쓰지 않고 차단한다.
선택하지 않은 overlay에는 현재 버전 pin을 추가할 수 있으나 실제 렌더링 결과는 유지한다.

## 실패 환경 재시도

```json
{"env": "gcp", "idempotency_key": "retry-1"}
```

위 본문을 인증된 `POST /deployment-requests/{id}/retry`로 보낸다.
GitOps 반영이 완료된 요청이고 해당 환경의 마지막 실제 알림이 degraded/sync_failed일 때만 허용한다.
멱등한 재시도 작업을 저장하고 그 환경 Rollout의 pod template에 재시도 annotation을 추가해 새 동기화를 유도한다.
재시도는 현재 GitOps overlay를 읽어 annotation 패치만 추가한다. 운영자가 추가한 리소스·설정·이미지 항목을 보존하며, 다른 환경에는 이미지 pin도 추가하지 않는다. 같은 재시도 key의 패치는 중복 추가하지 않는다.
성공한 환경과 이미지 버전은 유지한다. 이미 다른 버전을 배포한 환경은 이전 버전으로 되돌리지 않는다.
재시도 커밋도 실제 성공 알림 전까지 성공으로 표시하지 않는다. 새 key는 새 재시도를 뜻한다.

## 나중에 활성화하는 순서

현재 실제 GitOps ConfigMap·Application·overlay·이미지 태그는 변경하지 않았다.

1. API와 모든 worker를 새 코드로 업그레이드하되 `MULTI_TARGET_ENABLED=false`로 유지한다. 0013은 기존 인덱스를 유지하며 새 테이블·인덱스를 추가한다.
2. 배포 담당이 sample-app CI 변경을 확인하고 적용한다. 서비스 활성화와 repository variable `MULTI_TARGET_ENABLED`를 맞춘다.
3. 실제 준비된 클러스터만 `DEPLOYMENT_TARGETS_JSON` allowlist에 등록한다. API와 worker는 동일한 목록을 사용한다. gitops 예시는 실제 준비 상태를 증명하지 않는다.
4. API와 worker에서 `MULTI_TARGET_ENABLED=true`를 설정한다. 이때 기존 repo/head 전체 unique 인덱스를 제거한다. 새 단일 환경 인덱스와 부모/환경 unique 인덱스는 유지한다.
5. 별도 앱 PR에서 선택 파일·환경 명세를 연결해 환경별 실제 배포를 확인한다.

이전 worker와 새 worker를 섞은 채 기능을 활성화하지 않는다. 새 Kafka 필드와 DB unique 조건이 이전 버전과 다르다.
flag만 false로 되돌리면 DB 인덱스와 overlay 이미지 pin까지 복구되지는 않는다. 이전 이미지로의 롤백에는 진행 중 요청 중단과 별도 호환성 검토가 필요하다.

worker 이미지에는 checksum을 검증해 설치하는 kubectl을 포함한다. 로컬은 `KUBECTL_BIN`으로 경로를 바꿀 수 있다.
클러스터 API를 호출하지 않고 Kustomize 렌더링에만 사용한다. 설치 절차는 [Kubernetes 공식 문서](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/)를 따른다.
로컬 검증과 같은 v1.32.2/Kustomize v5.5.0을 기본 pin으로 썼다. 실제 배포 전에 이미지 빌드·Trivy와 선택할 바이너리 버전으로 렌더링을 다시 확인한다.

## 검증과 남은 실제 연결

각 패키지 디렉터리에서 PYTHONPATH를 ai/common/api/worker로 설정해 pytest를 실행한다.
`python scripts/test_multitarget_postgres.py`는 UTF-8·TCP 미사용 임시 PostgreSQL을 생성하고 검사 후 종료·삭제한다.

다중 환경 테스트는 세 환경 일괄 반영, GCP 단독 배포에서 비선택 환경의 동일 렌더링, 모든 환경 재검토, 중복/재시작, Draft, 보호 리소스, GitOps 경합, 부분 실패·단일 환경 재시도를 포함한다.
실DB 테스트는 전체 13개 마이그레이션, 비활성 상태의 이전 SQL 호환성, 환경별 중복 방지, 경합 잠금 해제와 전체 검토→릴리스를 확인한다.

GitHub REST·Kafka broker·실제 Argo/클러스터·GHCR 이미지 푸시·완성 worker Docker 이미지 빌드는 이 로컬 검증에 포함되지 않았다.
GCP 및 local의 실제 클러스터·권한·스토리지·접속 정책, Argo 알림 도착, 이미지 CI 계약은 담당자와 별도로 확인해야 한다.
관리형 DB/버킷/시크릿 프로비저닝은 기존 renderer의 blocking 정책을 유지한다.

## 세 브랜치 통합 검증에서 확인한 시연 제약

AI 개선·부산 정책·다중 환경 요청을 함께 적용한 회귀 테스트를 추가했다.
부산 수정 전 도쿄 승인 → 부산 수정 커밋 → 모든 환경 새 SHA 재검토 → 도쿄 재승인 순서이며,
이전 SHA 승인을 자동으로 이전하지 않는다. 승인 한 번이라는 대본은 이 순서를 반영해야 한다.

현재 도쿄 예시의 외부 PostgreSQL은 기존 renderer의 `DB_PROVISIONING_REQUIRED` 때문에
최종 preflight에서 blocked된다. 인프라가 DB를 준비하는 것만으로 코드의 차단이 해제되지는 않는다.
등록·검증된 기존 DB를 사용할 별도 계약을 AI·파이프라인·배포 담당자가 맞추거나,
이번 리허설 입력에서 DB 변경 장면을 제외해야 한다. 보호 게이트를 제거해 성공으로 만들지 않았다.

인증된 부모 진행 API는 구현되어 있지만, 기존 HTML 진행 화면의 부모 요청/세 환경 카드 집계는
이번 변경에 포함하지 않는다. 실패 관찰은 최근 최대 50건을 읽고 verdict에는 관여하지 않는다.
실제 실패 사례 화면은 데이터 저장·분석 소비·인증된 표시를 리허설에서 확인해야 한다.
