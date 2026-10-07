---
doc_type: incident
provider: any
title: IPv6 전용 VM 에서 Docker Hub pull 시간이 2~15분으로 들쭉날쭉해 첫 배포가 시간 초과
card_id: K-007
related_rules: []
observed: 2026-10-02 / gcp-vm (e2-micro, 외부 IPv6 만) / cloud-init 에이전트 설치·첫 배포
---
## 증상
같은 구성의 VM 인데 에이전트 이미지 node:22-alpine pull 이 2분·6.5분·11분·15분으로 달랐다. 같은 시각 rancher 시스템 이미지(역시 Docker Hub)는 50초. 첫 배포에서 postgres·cloudflared pull 만으로 StatefulSet 롤아웃 300초를 넘겨 실패(앱 Pod 는 Running).

## 원인
GitHub·ghcr.io 처럼 IPv6 가 없는 곳은 아예 못 가고, Docker Hub 는 IPv6 로 가지만 처리량이 일정하지 않다. 같은 서브넷 VM 들이 한 /64 를 공유해 익명 pull 한도도 함께 쓴다. e2-micro 의 디스크·CPU 가 압축 해제를 더 늦춘다.

## 고치는 법
공통 이미지를 같은 리전 Artifact Registry 로 미러(mirror.yml, amd64 만)하고 cloud-init 이 VM SA 토큰으로 받아 Docker Hub 이름으로 태그한다(kubelet IfNotPresent). small 노드는 롤아웃을 900초까지 기다린다.

## 확인
미러 적용 VM 에서 세 이미지 모두 미러에서 받음(69s·99s·38s), dispatch→등록 7m43s, 버튼 1번 배포 GCP 317s 성공.
