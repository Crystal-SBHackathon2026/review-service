-- 끝난 검토의 판단 사례. 다음 검토가 같은 규칙에 걸리면 근거 문서(chunk_id case:<case_id>)로 붙는다.
-- 워커가 종료 지점(사람 거절·AI 수정 커밋·사람 승인 후 gitops 커밋)에서, API 가 Argo CD Degraded 에서 넣는다.
-- 내용은 마스킹된 rounds·findings 에서만 만든다 (review_ai.cases) — 비밀 값·finding evidence 는 넣지 않는다.
CREATE TABLE review_cases (
    case_id     text PRIMARY KEY,         -- <review_id>.<outcome> — 같은 종료 지점이 두 번 돌아도 한 번만
    review_id   text NOT NULL REFERENCES reviews (review_id),
    app         text NOT NULL,
    target_env  text NOT NULL,
    rule_ids    text[] NOT NULL,
    outcome     text NOT NULL CHECK (outcome IN (
                    'auto_fixed', 'human_approved', 'human_edited', 'recommended', 'rejected', 'deploy_degraded')),
    summary     text NOT NULL,
    ops         jsonb NOT NULL DEFAULT '[]',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX review_cases_rule_ids_idx ON review_cases USING gin (rule_ids);
