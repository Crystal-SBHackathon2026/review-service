-- pull_request 웹훅으로 검토를 시작한다. 같은 PR 에 새 커밋이 오면 그 PR 의 끝나지 않은 검토를 superseded 로 넘긴다.
-- POST /reviews 로 직접 요청한 검토는 PR 번호를 모른다 (null).
ALTER TABLE reviews ADD COLUMN pr_number integer;
CREATE INDEX reviews_repo_head_idx ON reviews ((spec_ref->>'repository'), pr_head_sha);
CREATE INDEX reviews_repo_pr_idx ON reviews ((spec_ref->>'repository'), pr_number);
