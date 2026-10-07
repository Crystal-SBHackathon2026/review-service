---
doc_type: incident
provider: any
title: 서비스가 띄운 cloudflared 는 서비스를 멈출 때 같이 죽는다 — setsid 만으로는 부족하고 부모 트리에서 떼야 한다
card_id: K-008
related_rules: []
observed: 2026-10-02 / 제어 서비스(Mac) / 리허설 3일차 — 능력 선언을 다시 읽으려고 제어 서비스만 재시작
---
## 증상
제어 서비스를 재시작했더니 Quick Tunnel 주소가 바뀌어 PC·GCP·Azure 에이전트가 모두 옛 주소를 들고 끊겼다. bin/control 은 cloudflared 를 nohup ... & 로 따로 띄우고 pid 파일로 재사용하게 돼 있었다. setsid 로 새 세션에 띄운 뒤에도(PGID = PID 확인) 다시 재시작하자 같은 일이 났다.

## 원인
nohup 은 SIGHUP 만 막는다. 같은 프로세스 그룹이면 Ctrl-C·작업 중지가 그룹 전체에 신호를 보내고, setsid 로 그룹을 떼도 부모(exec 된 tsx)가 그대로라 프로세스 트리째 종료하는 도구(작업 중지·IDE)에 함께 걸린다. Quick Tunnel 은 다시 띄우면 주소가 바뀐다.

## 고치는 법
이중 fork + setsid 로 launchd(PPID 1) 밑에 띄우고 pid 는 자식이 직접 남긴다(macOS 는 setsid 명령이 없어 perl POSIX). 그래도 주소가 바뀌는 경우(K-005)에 대비해 제어 서비스가 시작할 때 지난 주소·에이전트 번들과 비교해 PC 에이전트는 새 1회용 토큰으로 자동 재설치, 원격 VM 은 stale 로 표시하고 재생성 명령을 남긴다(apps/control/src/reregister.ts).

## 확인
ps 에서 cloudflared PPID 1 · 제어 서비스 재시작 후 같은 주소 · 재등록 로그 없음 · PC online 유지.
