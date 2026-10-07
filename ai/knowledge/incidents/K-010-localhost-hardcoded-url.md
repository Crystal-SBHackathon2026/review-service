---
doc_type: incident
provider: any
title: 서버 리다이렉트·브라우저 API 주소에 localhost 를 하드코딩하면 공개 주소에서 동작하지 않는다
card_id: K-010
related_rules: []
observed: 2026-10-01 / pc / trap 브랜치 --force 배포 (1일차)
---
## 증상
폼 로그인 뒤 http://localhost:3000/ 으로 302 리다이렉트된다 — 공개 URL 사용자는 자기 PC 로 보내진다. 브라우저 코드의 API_BASE 도 localhost 라 화면에서 요청이 사용자 PC 로 간다.

## 원인
개발 PC 에서만 맞는 절대 주소를 코드에 넣었다. 터널·로드밸런서 뒤에서 앱은 자기 공개 주소를 모른다.

## 고치는 법
리다이렉트는 상대 경로(res.redirect('/')), 브라우저 API 는 같은 출처의 상대 경로(API_BASE = ''). 절대 주소가 꼭 필요하면 환경변수로 받는다. 프록시 뒤라면 app.set('trust proxy', 1).

## 확인
trap(--force) todo-login-redirect 실패("서비스 주소 하드코딩") · fixed-manual·good-sqlite 통과. 브라우저 API_BASE 는 런북이 화면을 쓰지 않아 검사로는 드러나지 않는다 — 탐지기로만 잡힌다.
