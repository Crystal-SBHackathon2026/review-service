-- deploy.yaml 이 없거나 비었거나 형식이 깨진 PR. reviews 는 app·target_env 가 필수라 명세를 모르는 입력을 못 담는다.
-- 웹훅이 processing 으로 넣고, API 가 생성 커밋(generated) 또는 거절(rejected)·오류(failed)로 끝낸다.
-- 생성 커밋은 PR 브랜치에 올라가 synchronize 웹훅으로 일반 검토가 된다 → 그 검토를 review_id 로 잇는다.
CREATE TABLE spec_intakes (
    intake_id         text PRIMARY KEY,
    repository        text NOT NULL,
    head_repository   text NOT NULL,  -- 다르면 포크 PR — 브랜치에 커밋할 수 없다
    pr_number         integer NOT NULL,
    head_sha          text NOT NULL,
    head_ref          text NOT NULL,
    path              text NOT NULL,
    kind              text NOT NULL CHECK (kind IN ('missing', 'empty', 'yaml_error', 'schema_error')),
    errors            jsonb NOT NULL DEFAULT '[]',  -- 파싱·검증 오류. 원문 값·코드 조각은 넣지 않는다
    status            text NOT NULL DEFAULT 'processing' CHECK (status IN (
                          'processing', 'generated', 'repaired', 'rejected', 'failed')),
    reason            text,           -- GENERATED·UNVERIFIED·REPAIR_UNAVAILABLE·LOOP_GUARD·FORK_PR 등
    message           text,
    details           jsonb NOT NULL DEFAULT '[]',
    result_commit_sha text,
    review_id         text REFERENCES reviews (review_id),
    requested_by      text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (repository, head_sha)    -- 같은 커밋에 웹훅이 두 번 와도 한 번만 처리한다
);

CREATE INDEX spec_intakes_result_commit_idx ON spec_intakes (repository, result_commit_sha);
CREATE INDEX spec_intakes_head_sha_idx ON spec_intakes (head_sha);
CREATE INDEX spec_intakes_processing_idx ON spec_intakes (updated_at) WHERE status = 'processing';
