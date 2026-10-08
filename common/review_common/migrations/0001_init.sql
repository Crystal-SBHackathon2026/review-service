-- 업무 DB 초기 스키마. 상태값·사유 코드는 review-pipeline-tasks.md 3절 기준.
-- LangGraph 체크포인터 테이블은 여기서 만들지 않는다 — 워커가 AsyncPostgresSaver.setup() 으로 만든다.

CREATE TABLE reviews (
    review_id         text PRIMARY KEY,
    app               text NOT NULL,
    target_env        text NOT NULL CHECK (target_env IN ('aws', 'gcp', 'local')),
    repo_id           text NOT NULL,
    spec_ref          jsonb NOT NULL,
    pr_head_sha       text NOT NULL,
    merge_sha         text,
    status            text NOT NULL DEFAULT 'received' CHECK (status IN (
                          'received', 'reviewing', 'needs_human', 'waiting_ci', 'merging',
                          'committed', 'blocked', 'rejected', 'failed')),
    verdict           text CHECK (verdict IN ('pass', 'fix', 'needs_human')),
    reasons           text[] NOT NULL DEFAULT '{}' CHECK (reasons <@ ARRAY[
                          'CITATION_INVALID', 'LOW_SCORE', 'AUTOFIX_FORBIDDEN', 'PATCH_OUT_OF_SCOPE',
                          'PATCH_MISSING', 'IRREVERSIBLE', 'LLM_UNAVAILABLE', 'LOOP_EXHAUSTED']::text[]),
    decision          jsonb,
    findings          jsonb,
    rounds            jsonb,  -- 고쳐서 통과한 검토의 설명과 근거는 여기에만 남는다
    human_decision    jsonb,
    deploy_result     jsonb,
    gitops_commit_sha text,
    final_spec        jsonb,  -- 적용한 ops 까지 반영한 명세(baseline 제외). 배포 Healthy 때 baselines 로 옮긴다
    error             text,   -- status=failed 일 때 원인
    requested_by      text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX reviews_pr_head_sha_idx ON reviews (pr_head_sha);
CREATE INDEX reviews_app_env_merge_sha_idx ON reviews (app, target_env, merge_sha);

CREATE TABLE baselines (
    app               text NOT NULL,
    target_env        text NOT NULL CHECK (target_env IN ('aws', 'gcp', 'local')),
    spec              jsonb NOT NULL,  -- ops 를 적용한 최종 명세
    spec_ref          jsonb NOT NULL,
    merge_sha         text,
    database_has_data boolean DEFAULT NULL,  -- 모르면 null. 규칙은 null 을 '데이터 있음'으로 본다
    observed_at       timestamptz,
    PRIMARY KEY (app, target_env)
);

CREATE TABLE deploy_events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    review_id   text REFERENCES reviews (review_id),
    app         text NOT NULL,
    target_env  text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('healthy', 'degraded')),
    image_tag   text,
    payload     jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX deploy_events_review_id_idx ON deploy_events (review_id);
