-- intake 가 baseline 없이 만든 명세의 검토는 pass 여도 사람 확인으로 보낸다 (10/09 sample-app #11 ingress 삭제 장애).
-- 사유 코드 GENERATED_SPEC_UNVERIFIED 를 받고, intake 가 baseline 으로 만들었는지 남긴다.
-- baseline_used 는 생성 커밋 SHA 와 같이(브랜치를 옮기기 전에) 기록한다. NULL 은 0006 전 행 — baseline 없음으로 본다.
ALTER TABLE reviews DROP CONSTRAINT reviews_reasons_check;
ALTER TABLE reviews ADD CONSTRAINT reviews_reasons_check CHECK (reasons <@ ARRAY[
    'CITATION_INVALID', 'LOW_SCORE', 'AUTOFIX_FORBIDDEN', 'PATCH_OUT_OF_SCOPE',
    'PATCH_MISSING', 'IRREVERSIBLE', 'LLM_UNAVAILABLE', 'LOOP_EXHAUSTED', 'GENERATED_SPEC_UNVERIFIED']::text[]);
ALTER TABLE spec_intakes ADD COLUMN baseline_used boolean;
CREATE INDEX spec_intakes_pr_idx ON spec_intakes (repository, pr_number);
