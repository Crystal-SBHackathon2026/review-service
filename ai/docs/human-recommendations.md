# 사람 응답의 미입력 권장값 적용

`needs_human` 결과의 `decision.recommendations`는 설명과 실제 적용할 JSON Patch를 함께 제공한다.
각 항목은 `{finding_ids, source, why, ops}`이다. 판단 시에는 제안만 저장하고, 승인 응답을 받은 뒤 적용한다.

현재 권장값의 근거:

- `validated_patch`: 기존 패치·인용 검증을 통과한 LLM 수정값. 다른 사람이 정할 문제와 섞여 자동 적용되지 않았어도 제안을 보존한다.
- `rule`: SQLite의 replicas 1, 공개 버킷의 public false 등 규칙으로 확정할 수 있는 값.
- `baseline`: 데이터가 있는 DB 엔진 변경·다운그레이드는 이전 승인 명세의 엔진·버전·배치·볼륨 설정 유지, 볼륨 축소는 기존 크기 유지.

baseline은 같은 앱·레포·대상 환경의 승인 명세여야 한다. 모든 후보는 형식 검사와 정적 검사를 거쳐 대상 문제를 없애고 새 문제를 만들지 않아야 표시된다. 시크릿 값, 존재 여부를 모르는 probe 경로, 빌드 플랫폼은 추측하지 않는다.

## 승인 API

전체 권장값 적용:

```json
{"decision": "approved", "approver": "reviewer"}
```

입력한 엔진을 우선 적용하고, 입력하지 않은 버전 등은 권장값으로 보충:

```json
{
  "decision": "approved",
  "approver": "reviewer",
  "edited_ops": [{"op": "replace", "path": "/database/engine", "value": "postgres"}]
}
```

- `use_recommendations`의 기본값은 `true`이다.
- 항목의 op 자체가 없거나 `value`가 없거나 빈/공백 문자열이면 미입력이다. 대응하는 권장값을 사용한다.
- `false`, `0`, 명시적인 `null`, `remove`는 사용자 입력이다. 형식·정적 검사는 그대로 적용된다.
- 같은 경로 또는 그 객체 전체를 명시하면 사용자 선택이 우선이다. 권장 객체 내부의 특정 필드만 입력한 경우에는 권장 객체 적용 후 해당 입력을 적용한다.
- 미입력 항목에 권장값이 없으면 `422`, 워커에 직접 전달됐다면 `needs_human`으로 다시 대기한다.
- 미입력 경로도 권장값 적용 후 문서에서 실제로 존재해야 한다. 권장 필드를 포함하는 상위 객체나 권장 객체의 실제 하위 필드는 허용하지만, `/size/typo` 같은 스칼라의 하위 경로·없는 키·범위 밖 배열 인덱스는 `422`로 거절한다.
- 적용 가능한 권장값을 보충해도 다른 문제가 남으면 재검사 후 `needs_human`으로 돌아간다.
- `rejected` 응답에는 권장값을 적용하지 않는다.
- 기존 명세를 수정 없이 승인하려면 `use_recommendations: false`를 명시한다. 이 경우에도 명시한 `edited_ops`는 적용·재검사한다.

이 정책은 **승인 응답 안에서 답하지 않은 항목**에 대한 것이다. 승인 응답 자체가 도착하지 않은 검토를 시간 경과로 승인하는 타이머는 없다.

## 파이프라인 연결과 기록

API는 업무 DB에 저장된 `decision.recommendations`로 병합 결과를 미리 검증한다. 클라이언트가 권장값을 별도로 만들어 보내지 않는다.
Kafka의 `review.resumed/v1`은 생략된 `value`와 명시적인 `null`을 구분해 보존한다.

워커도 같은 병합 함수를 사용한다. 실제 입력과 권장값을 합친 `edited_ops`를 `human_decision`에 저장하고 `apply_human_edits → static_check`부터 재검사한다.
회차의 `patch.ops`에는 실제 적용값 전체가, `defaulted_ops`에는 권장값으로 보충한 항목이 남는다.
`defaulted_ops`는 서버 기록용이며 승인 API의 입력으로 받을 수 없다.
기존 `verdict.applied_ops(state)`로 사용자 입력과 보충값을 모두 원본 YAML에 커밋할 수 있다.

별도 DB 열이나 마이그레이션은 필요 없다. 기존 decision·human_decision·rounds JSON에 저장한다.
