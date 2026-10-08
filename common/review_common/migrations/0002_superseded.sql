-- AI 수정 커밋: 고쳐서 통과한 검토는 PR 브랜치에 deploy.yaml 수정을 커밋하고, 그 커밋을 새 검토(autofix_commit)로 넘긴다.
-- 넘긴 검토는 superseded 로 끝나고 superseded_by 에 새 review_id 를 남긴다.
ALTER TABLE reviews DROP CONSTRAINT reviews_status_check;
ALTER TABLE reviews ADD CONSTRAINT reviews_status_check CHECK (status IN (
    'received', 'reviewing', 'needs_human', 'waiting_ci', 'merging',
    'committed', 'blocked', 'rejected', 'failed', 'superseded'));
ALTER TABLE reviews ADD COLUMN superseded_by text REFERENCES reviews (review_id);
