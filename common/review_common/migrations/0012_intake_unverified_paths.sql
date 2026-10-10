-- intake 가 레포로 확인하지 못해 후보값으로 채운 명세 경로 (/image·/runtime·/database 등).
-- 예전에는 확인 못 한 값이 하나라도 있으면 커밋하지 않았다(UNVERIFIED). 이제 후보값으로 커밋하고 그 PR 의 검토를
-- needs_human(GENERATED_SPEC_UNVERIFIED) 으로 보낸다. 빈 배열이면 전부 확인된 생성 명세 — 일반 검토처럼 자동 진행한다.
-- NULL 은 이 마이그레이션 전 행 — 무엇을 확인했는지 모르니 예전처럼 사람 확인으로 본다.
-- 번호 0010·0011 은 열린 PR(intake attempts·deployment analysis)이 쓴다.
ALTER TABLE spec_intakes ADD COLUMN unverified_paths text[];
