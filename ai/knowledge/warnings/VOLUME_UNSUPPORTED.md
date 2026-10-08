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
- aws: EBS CSI 드라이버가 없어(애드온 4개 고정, Terraform) 어떤 볼륨도 만들 수 없다.
- gcp: ReadWriteOnce(PD). ReadWriteMany 는 Filestore 를 붙여야 한다.
- local: ReadWriteOnce(k3s local-path).

## 고치는 법
1. 볼륨을 지원하는 환경으로 배포한다.
2. aws 에 볼륨이 꼭 필요하면 인프라 쪽에서 EBS CSI 애드온을 추가하고 능력표(catalog/targets.yaml)를 실측해 고친다.
3. 상태를 외부로 옮긴다 (관리형 DB·버킷). 볼륨을 지우거나 비영속으로 바꾸는 것은 데이터를 잃을 수 있어 사람이 정한다 (STO-006).

## 확인
`kubectl get pvc` 가 Bound 이고 Pod 가 Running 이다.
