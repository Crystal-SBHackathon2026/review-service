---
doc_type: incident
provider: any
title: 연결이 끊긴 Cloudflare Quick Tunnel 은 지워지고, cloudflared 는 살아 있는 채로 영원히 재시도한다
card_id: K-005
related_rules: []
observed: 2026-10-02 / 제어 서비스(Mac) / 리허설 2일차 — 노트북 배터리 잠자기 뒤
---
## 증상
제어 서비스 공개 주소가 DNS 에서 사라지고(curl exit 6) 모든 에이전트가 끊긴다. cloudflared 프로세스는 살아 있고 로그에 'Register tunnel error ... Unauthorized: Tunnel not found' 가 반복된다. 새로 만든 터널도 잠자기 한 번(약 16분 끊김) 뒤 같은 상태가 됐다.

## 원인
Quick Tunnel 은 계정 없는 임시 터널이라 엣지 연결이 일정 시간 끊기면 Cloudflare 가 터널을 삭제한다. cloudflared 는 같은 터널 ID 로 재등록만 시도하므로 스스로 회복하지 못한다. 프로세스 생존 확인으로는 감지되지 않는다.

## 고치는 법
터널 로그에서 'Tunnel not found' 를 보면 터널을 새로 만든다(bin/control). 주소가 바뀌므로 기존 타깃은 oneaction target reinstall 로 같은 타깃에 새 1회용 토큰을 받아 에이전트를 다시 설치한다. 근본 대책은 고정 주소(Named Tunnel) — 2차 백로그 1번. 리허설 중에는 제어 서비스 Mac 을 잠들지 않게 둔다.

## 확인
재생성 후 공개 /healthz 200, PC 에이전트 reinstall 뒤 online.
