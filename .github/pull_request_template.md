## 무엇을 왜

<!-- 한두 문장. 관련 결정(D·A 번호)·Slack 스레드가 있으면 링크 -->

## 확인

- [ ] `ai-ci` 통과 (테스트·샘플 정합성·평가셋)
- [ ] 실제 Claude 로 돌려 봤다면 결과 요약: <!-- run_eval.py --llm claude 의 match_rate·비용, 안 돌렸으면 지우기 -->

## 해당하면 체크 (해당 없는 줄은 지워도 됩니다)

**접점이 바뀜 — 다른 담당에게 영향**
- [ ] `ReviewState`·Kafka 메시지·노드 시그니처를 바꿨다 → `ai/docs/review-ai.md` "연결 지점" 표를 고쳤고, 파이프라인·배포 담당에게 알렸다
- [ ] 렌더러 경고 코드를 추가·변경했다 → `blocking` 여부를 `review-ai.md` 경고 표에 적었다 (커밋 단계는 `rendered.blocking` 만 본다)

**규칙·패치**
- [ ] 규칙을 추가했다 → `catalog/rules.yaml` + `knowledge/rules/any/<ID>.md` + 샘플 또는 `eval/cases.yaml` 케이스
- [ ] 패치 허용 경로(`RULE_PATCH_PATHS`)를 넓혔다 → env·이미지·시크릿·네트워크 경로가 섞이지 않았다
- [ ] `DeploySpec` 을 바꿨다 → `validate_samples.py` 로 `schema/deploy_spec.schema.json` 을 다시 만들어 커밋했다

**근거 문서 (`knowledge/`)**
- [ ] 문서를 고쳤다 → 머지 후 `scripts/sync_knowledge_s3.sh <버킷> --apply` 와 `index_knowledge.py` 재색인
- [ ] 개수가 맞는다: 로컬 문서 수 = S3 객체 수, 청크 수 = Qdrant points (PR 본문에 숫자 적기)

**비밀**
- [ ] 로그·리포트·테스트 픽스처에 실제 키·토큰이 없다 (`eval/reports/` 는 커밋하지 않는다)
