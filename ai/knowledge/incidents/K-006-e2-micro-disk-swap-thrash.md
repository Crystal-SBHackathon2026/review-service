---
doc_type: incident
provider: any
title: 1GB e2-micro 에서 디스크 swap·상주 에이전트 때문에 메모리 스래싱 → k3s API 서버 시간 초과
card_id: K-006
related_rules: []
observed: 2026-10-02 / gcp-vm (e2-micro, pd-standard 10GB, Ubuntu minimal 24.04) /
  첫 배포
---
## 증상
부팅 → 에이전트 등록까지 11분(k3s Ready 154초, multipass 1GB 는 13초). 첫 배포가 매니페스트 적용 중 'k8s PATCH ... 시간 초과' 로 실패. k3s 로그에 apiserver 'Handler timeout', etcd(kine) 재시도.

## 원인
Cloud Monitoring 실측: 유휴 상태에서도 디스크 읽기 초당 약 4.5MB·265 IOPS, CPU 70~80% — 메모리가 부족해 실행 파일 페이지가 계속 밀려났다가 디스크에서 다시 읽힌다. GCP 이미지는 multipass 보다 상주 프로세스 (osconfig 에이전트 등)가 많고 디스크 swap 이 같은 느린 표준 PD 를 쓴다. multipass 1GB 실측만으로는 드러나지 않았다.

## 고치는 법
swap 을 zram(압축 RAM)으로, google-osconfig-agent·snapd·unattended-upgrades·k3s helm-controller 끄기, 표준 PD 를 상시 무료 상한 30GB 로(성능이 크기에 비례), 에이전트 SSA apply 를 시간 초과·5xx 에 재시도.

## 확인
같은 구성으로 재생성한 VM 에서 부팅→등록 시간과 배포 성공, 시리얼 콘솔 oneaction-diag 메모리 기록.
