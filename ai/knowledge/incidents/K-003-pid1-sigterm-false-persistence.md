---
doc_type: incident
provider: any
title: 재시작 후 보존 검사가 종료 중인 이전 Pod 에 응답받아 거짓 통과한다
card_id: K-003
related_rules: []
observed: 2026-10-01 / pc / 검증
---
## 증상
/tmp 에 SQLite 를 쓰는 trap 앱이 rollout restart 후에도 "marker 있음" 으로 보존 검사를 통과했다.

## 원인
(1) Node 가 PID 1 이고 SIGTERM 핸들러가 없어 신호를 무시 → 이전 Pod 가 유예 30초 동안 계속 산다. (2) rollout status 는 새 Pod 준비만 보고 끝난다. (3) 터널의 기존 keep-alive 연결이 이전 Pod 로 간다.

## 고치는 법
재시작 전 Pod 이름을 기록하고 `kubectl wait --for=delete` 로 모두 사라진 뒤 읽는다. 앱에는 SIGTERM 핸들러(서버 close → DB 종료)를 둔다.

## 확인
trap 보존 검사가 "재시작 후 로그인 401" 로 실패, 재시작 33s 로 느린 종료가 드러남. fixed-manual 9s.
