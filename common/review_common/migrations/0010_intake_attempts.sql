-- intake 처리 시도 횟수. 웹훅이 넣을 때 1, sweep 이 다시 가져갈 때마다 +1.
-- 처리하다 파드를 죽이는 intake(npm 잠금 파일 재생성·대용량 분석 OOM 등)를 sweep 이 STALE_AFTER 마다 끝없이 다시 가져가지 않게
-- API 가 상한(MAX_INTAKE_ATTEMPTS)을 넘은 행은 처리하지 않고 failed(RETRY_EXHAUSTED)로 끝낸다. 이 마이그레이션 전 행은 1.
ALTER TABLE spec_intakes ADD COLUMN attempts integer NOT NULL DEFAULT 1 CHECK (attempts >= 1);
