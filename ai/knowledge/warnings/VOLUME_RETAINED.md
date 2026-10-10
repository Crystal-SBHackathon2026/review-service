---
warning_code: VOLUME_RETAINED
doc_type: warning
provider: aws
blocking: false
title: 영속 볼륨이 Retain 클래스라 PVC 를 지워도 디스크와 과금이 남는다
---
## 왜 알리나
대상 환경의 StorageClass reclaimPolicy 가 Retain 이면 PVC 를 지워도 PV 와 실제 디스크(aws 는 EBS)가 지워지지 않는다.
데이터를 실수로 잃지 않는 대신, 앱을 내리거나 볼륨을 바꾼 뒤 남은 디스크가 계속 과금된다. 배포는 그대로 진행한다.

## 환경별
- aws: `oneaction-monitoring-gp3` (EBS gp3, Retain, WaitForFirstConsumer). 2026-10-10 실측 — Prometheus 10Gi·Grafana 2Gi PVC 가 같은 클래스로 Bound.
  정의는 인프라 레포 `infra/eks-storage/manifests/storage-class.yaml` 에 있다(gitops·terraform 에는 없다).
  EBS 는 단일 AZ 저장소다. WaitForFirstConsumer 라 PV 는 첫 Pod 가 놓인 노드의 AZ 에 만들어지고 그 AZ 에 묶인다 —
  그 AZ 의 노드가 없으면 Pod 가 뜨지 않는다.
- gcp·local: 기본 클래스(Delete) — 이 경고는 나오지 않는다. 대신 PVC 를 지우면 데이터도 사라진다(STO-006).

## 같이 볼 것
- ReadWriteOnce 는 "한 노드" 단위다. canary·bluegreen 은 새 Pod 와 옛 Pod 가 잠시 같이 뜨는데, 둘이 다른 노드에 놓이면
  새 Pod 가 볼륨을 붙이지 못한다(Multi-Attach). 렌더러는 RWO PVC 가 있으면 Rollout Pod 에 같은 노드 podAffinity(required)를,
  데이터 이전 Job 에 preferred 를 넣는다. 그 노드에 자리가 없으면 새 Pod 가 Pending → progressDeadlineAbort 로 중단되고
  옛 버전이 계속 서비스한다 — 노드 자원(requests)을 줄이거나 노드를 늘린다.
- 여러 replica 가 같은 볼륨을 쓰면 STO-002 가 막는다. 공유 저장소·여러 AZ 동시 마운트(ReadWriteMany)는 EBS 로 안 되며 EFS 등을 따로 검토한다.
- 용량은 명세의 storage.volumes[].size 가 앱별로 정한다. 줄일 수 없다(STO-005).

## 정리 (담당)
1. PVC 삭제는 앱 담당자가 결정하고 gitops 에서 직접 지운다 — 검토 서비스는 pvc-*.yaml 을 지우는 커밋을 하지 않는다(OVERLAY_RESOURCE_REMOVED).
   지우기 전에 데이터 보존 여부와 그 볼륨을 쓰는 Pod 가 모두 종료됐는지 확인한다.
2. 남은 PV·EBS 볼륨은 인프라 담당(Terraform·AWS 계정)이 PV↔EBS 매핑과 연결 해제(EBS available) 상태를 확인한 뒤 지운다.
   `kubectl get pv` 에서 STATUS Released 인 PV 의 `spec.csi.volumeHandle` 이 EBS 볼륨 ID 다. (2026-10-10 박찬건 합의)

## 확인
`kubectl get pvc` 가 Bound 이고 Pod 가 Running 이며, PV 의 nodeAffinity AZ 와 Pod 노드의 AZ 가 같다.
canary·bluegreen 중에는 새 Pod 가 옛 Pod 와 같은 노드에 놓였는지, 이벤트에 Multi-Attach 오류가 없는지, 그 노드에
새 Pod 자리(Pod 수·requests 여유)가 있는지 본다. 정리 뒤에는 Released PV 와 available 상태 EBS 볼륨이 남아 있지 않다.
