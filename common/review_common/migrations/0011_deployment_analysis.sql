-- Additive rollout: old emitters/workers continue to work. Evidence is immutable per event.
ALTER TABLE deploy_events DROP CONSTRAINT deploy_events_kind_check;
ALTER TABLE deploy_events ADD CONSTRAINT deploy_events_kind_check CHECK (kind IN ('healthy', 'degraded', 'sync_failed'));
ALTER TABLE reviews ADD COLUMN case_advice jsonb;
CREATE TABLE deployment_observations (
    event_id text PRIMARY KEY,
    attempt_key text NOT NULL,
    app text NOT NULL,
    target_env text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('deployed', 'health_degraded', 'sync_failed')),
    review_id text REFERENCES reviews(review_id),
    repository text,
    spec_snapshot jsonb,
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX deployment_scope_idx ON deployment_observations(app, target_env, occurred_at DESC);
CREATE INDEX deployment_review_idx ON deployment_observations(review_id, kind);
-- Idempotent projection into the existing progress/Grafana feed, including cross-env notifications.
ALTER TABLE deploy_events ADD COLUMN deployment_event_id text UNIQUE REFERENCES deployment_observations(event_id);
CREATE INDEX deployment_attempt_idx ON deployment_observations(attempt_key) WHERE kind != 'deployed';
CREATE TABLE deployment_analysis_jobs (
    event_id text PRIMARY KEY REFERENCES deployment_observations(event_id),
    status text NOT NULL CHECK (status IN ('pending', 'queued', 'processing', 'completed', 'insufficient', 'failed', 'skipped')),
    attempts integer NOT NULL DEFAULT 0,
    lease_token text,
    lease_until timestamptz,
    next_publish_at timestamptz NOT NULL DEFAULT now(),
    result jsonb,
    error_code text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX deployment_jobs_due_idx ON deployment_analysis_jobs(next_publish_at)
    WHERE status IN ('pending', 'queued', 'processing');
CREATE TABLE deployment_failure_cases (
    case_id text PRIMARY KEY REFERENCES deployment_observations(event_id),
    app text NOT NULL,
    repository text NOT NULL,
    target_env text NOT NULL,
    failed_spec jsonb NOT NULL,
    diagnosis jsonb NOT NULL,
    resolution jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX deployment_cases_scope_idx ON deployment_failure_cases(app, repository, target_env, created_at DESC);
