---
doc_type: guide
provider: any
title: Argo Rollouts 헬스 게이트와 자동 중단
related_rules: [RUN-001]
---
## 동작
gitops 의 Rollout 은 progressDeadlineSeconds 60, progressDeadlineAbort true 로 설정돼 있다. 새 버전 Pod 가 60초 안에
readiness 를 통과하지 못하면 배포를 멈추고 기존(stable) 버전을 계속 서비스한다.

## 한계
readiness 를 통과해도 쓰기·읽기 같은 기능이 깨질 수 있다. readiness 는 최소 조건이고, 기능 검사(AnalysisTemplate)가 있어야
"준비됐지만 동작하지 않는" 릴리스를 잡는다. 지금 gitops 에는 AnalysisTemplate 이 없다.
