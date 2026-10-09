-- 같은 레포·head SHA 의 검토는 하나만 (P2). 같은 웹훅이 동시에 두 번 오면 둘 다 find_by_head 를 지나 검토가 2개 생겼다.
-- failed·superseded 는 뺀다 — failed 는 같은 SHA 가 다시 오면 새로 검토하고(P1-4), superseded 는 병합 없이 닫혔다가
-- 다시 열린 PR 의 같은 SHA 를 다시 검토한다. insert_review 는 겹치면 넣지 않고 기존 review_id 를 돌려준다.
-- 적용 전 확인 (0 행이어야 한다):
--   SELECT repo_id, pr_head_sha, array_agg(review_id ORDER BY created_at), array_agg(status ORDER BY created_at)
--   FROM reviews WHERE status NOT IN ('failed', 'superseded') GROUP BY 1, 2 HAVING count(*) > 1;
CREATE UNIQUE INDEX reviews_repo_head_open_uniq ON reviews (repo_id, pr_head_sha)
    WHERE status NOT IN ('failed', 'superseded');
