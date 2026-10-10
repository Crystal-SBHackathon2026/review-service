-- 기존 단일 환경 검토는 그대로 유지하고, 묶음의 자식 검토는 환경별로 식별한다.
CREATE TABLE deployment_requests (
    request_id text PRIMARY KEY,
    repository text NOT NULL,
    pr_number integer NOT NULL,
    head_sha text NOT NULL,
    manifest_path text NOT NULL,
    targets jsonb NOT NULL,
    requested_by text NOT NULL,
    state text NOT NULL DEFAULT 'reviewing',
    fix_count integer NOT NULL DEFAULT 0,
    pending_sha text,
    merge_sha text,
    gitops_commit_sha text,
    expected_snapshot jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(repository, pr_number, head_sha, manifest_path)
);
ALTER TABLE reviews ADD COLUMN deployment_request_id text REFERENCES deployment_requests(request_id);
-- 기존 인덱스는 비활성 모드 및 이전 이미지와의 호환성을 위해 유지한다.
-- 모든 API/worker 업그레이드 후 MULTI_TARGET_ENABLED=true일 때 별도로 제거한다.
CREATE UNIQUE INDEX reviews_single_head_open_uniq ON reviews(repo_id, pr_head_sha)
    WHERE status NOT IN ('failed', 'superseded') AND deployment_request_id IS NULL;
CREATE UNIQUE INDEX reviews_request_env_uniq ON reviews(deployment_request_id, target_env)
    WHERE deployment_request_id IS NOT NULL;
CREATE INDEX deployment_requests_pending_idx ON deployment_requests(state, updated_at);

CREATE TABLE deployment_request_retries (
    retry_id text PRIMARY KEY,
    request_id text NOT NULL REFERENCES deployment_requests(request_id),
    target_env text NOT NULL CHECK (target_env IN ('aws','gcp','local')),
    state text NOT NULL DEFAULT 'pending',
    gitops_commit_sha text,
    error text,
    created_at timestamptz NOT NULL DEFAULT now()
);
