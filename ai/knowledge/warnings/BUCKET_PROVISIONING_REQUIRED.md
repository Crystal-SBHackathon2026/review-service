---
warning_code: BUCKET_PROVISIONING_REQUIRED
doc_type: warning
provider: any
blocking: true
title: storage.buckets 는 overlay 로 만들지 않아 인프라 쪽 반영이 먼저다
---
## 왜 멈추나
버킷은 클러스터 밖(S3·GCS) 자원이라 Kubernetes overlay 로 만들 수 없다. 앱이 없는 버킷에 쓰면 런타임에 실패하므로 커밋 단계에서 멈춘다.

## 고치는 법
Terraform 으로 버킷을 만든다. 공개 접근 차단(STO-003)·암호화(STO-004)를 명세와 같게 맞추고,
앱이 쓸 권한(aws Pod Identity·gcp Workload Identity)과 버킷 이름을 시크릿 또는 env 로 넘긴다.

## 확인
`aws s3api head-bucket --bucket <이름>`(또는 `gcloud storage buckets describe`)이 성공하고, 공개 접근 차단 설정이 켜져 있다.
