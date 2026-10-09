-- 커밋 타임라인 대시보드(Grafana, 업무 DB) — 검토 단계별 시각. 생성(created_at) → 판정 → 사람 결정 → 병합 → gitops 커밋
-- → 배포 알림(deploy_events.received_at). 이 마이그레이션 전 행은 비어 있다.
ALTER TABLE reviews ADD COLUMN judged_at timestamptz;          -- 마지막 판정 (record_result)
ALTER TABLE reviews ADD COLUMN human_decided_at timestamptz;   -- needs_human 에 사람이 승인·거절한 시각
ALTER TABLE reviews ADD COLUMN merged_at timestamptz;          -- 워커가 PR 을 병합한 시각
ALTER TABLE reviews ADD COLUMN gitops_committed_at timestamptz; -- gitops overlay 커밋(committed) 시각
