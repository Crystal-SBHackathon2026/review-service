---
doc_type: incident
provider: any
title: Dockerfile 이 베이스 이미지를 한 아키텍처로 고정하면 다른 아키텍처 타깃에서 실행되지 않는다
card_id: K-013
related_rules:
- RUN-004
observed: 설계된 함정 — trap 브랜치 (실측 없음, trap 은 성적표에서 차단돼 amd64 타깃에 배포되지 않았다)
---
## 증상
예상: amd64 타깃(GCP e2-micro·Azure B2ls_v2)에서 컨테이너가 exec format error 로 CrashLoop. arm64 인 PC(Apple Silicon)·AWS t4g 에서는 드러나지 않는다.

## 원인
FROM --platform=linux/arm64 는 빌드 대상 플랫폼과 무관하게 arm64 베이스를 쓴다. 멀티 아키텍처 빌드의 amd64 결과물에도 arm64 node 가 들어간다. 함께 있던 npm install(lockfile 무시 가능)·root 실행은 재현성·보안 문제다.

## 고치는 법
--platform 고정을 지운다(필요하면 $BUILDPLATFORM 은 빌드 단계에만). RUN npm ci --omit=dev, USER node.

## 확인
미측정. fixed-manual 은 amd64·arm64 두 아키텍처로 빌드해 4개 타깃(arm64 2·amd64 2)에서 같은 digest 로 동작(3일차 d-f7390e4f9619).
