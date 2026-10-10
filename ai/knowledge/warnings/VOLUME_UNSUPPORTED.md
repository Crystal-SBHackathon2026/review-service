---
warning_code: VOLUME_UNSUPPORTED
doc_type: warning
provider: any
blocking: true
title: 대상 환경에 그 접근 모드의 영속 볼륨을 만들 스토리지가 없다
---
## 왜 멈추나
persistent 볼륨의 access_mode 를 대상 환경이 지원하지 않으면 PVC 가 Pending 에 머물고 Pod 가 뜨지 않는다.
검토 단계의 STO-001 과 같은 기준이다 — 렌더러가 커밋 직전에 한 번 더 막는다.

## 환경별
- aws: ReadWriteOnce(EBS gp3, StorageClass oneaction-monitoring-gp3 — 2026-10-10 실측). ReadWriteMany 는 EFS 를 붙여야 한다.
- gcp: ReadWriteOnce(PD). ReadWriteMany 는 Filestore 를 붙여야 한다.
- local: ReadWriteOnce(k3s local-path).

## 고치는 법
1. 볼륨을 지원하는 환경으로 배포한다.
2. ReadWriteMany 가 꼭 필요하면 인프라 쪽에서 공유 스토리지(aws EFS·gcp Filestore)를 붙이고 능력표(catalog/targets.yaml)를 실측해 고친다.
   아니면 ReadWriteOnce + replicas 1 로 낮춘다.
3. 상태를 외부로 옮긴다 (관리형 DB·버킷). 볼륨을 지우거나 비영속으로 바꾸는 것은 데이터를 잃을 수 있어 사람이 정한다 (STO-006).

## 확인
`kubectl get pvc` 가 Bound 이고 Pod 가 Running 이다.
