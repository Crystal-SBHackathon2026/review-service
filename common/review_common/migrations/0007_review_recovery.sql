-- 멈춘 검토 회수(review sweep, API). received·reviewing·merging 이 REVIEW_STALE_AFTER 넘게 그대로면 다시 발행한다.
-- 회수할 때마다 recover_count 를 올리고, MAX_RECOVERIES(3) 를 넘으면 failed 로 끝낸다.
ALTER TABLE reviews ADD COLUMN recover_count integer NOT NULL DEFAULT 0;
CREATE INDEX reviews_status_updated_at_idx ON reviews (status, updated_at);
