"""배포 계획 — 스키마·저장소 변경으로 마이그레이션 실행 시점과 배포 전략을 정한다 (설계 '앱 분석/변환' ⑤).

배포 중에는 옛 버전과 새 버전이 잠깐 같이 돈다(canary 는 단계마다 몇 분, bluegreen 은 미리보기 확인 동안).
그 구간에 두 버전이 같은 스키마·같은 저장소에서 돌 수 있는지가 기준이다.

| 변경 | 마이그레이션 hook | 전략 |
|---|---|---|
| none · expand (추가만) | PreSync — 새 버전보다 먼저. 옛 코드는 추가된 컬럼을 무시한다 | canary |
| contract (제거만) | PostSync — 새 버전이 다 뜬 뒤. 새 코드는 지울 컬럼을 이미 안 쓴다 | canary |
| breaking (이름·타입 변경) | PreSync | bluegreen. 그래도 마이그레이션 뒤 전환 전까지 옛 버전이 바뀐 스키마로 돈다 → 사람 승인 (RUN-006) |
| 이전 배포와 DB 엔진·배치가 다름 | — | bluegreen. canary 동안 두 버전이 서로 다른 DB 에 써서 데이터가 갈린다 (RUN-007) |
"""

from __future__ import annotations

from typing import Literal

from review_ai.spec.deploy_spec import AppSpec, SchemaChange, Strategy

Hook = Literal["PreSync", "PostSync"]


def migration_hook(change: SchemaChange) -> Hook:
    """contract 만 새 버전이 다 뜬 뒤(PostSync). 나머지는 새 버전보다 먼저(PreSync)."""
    return "PostSync" if change == "contract" else "PreSync"


def mixed_versions_unsafe(spec: AppSpec, previous: AppSpec | None) -> str | None:
    """옛 버전과 새 버전이 함께 돌면 안 되는 이유. 함께 돌아도 되면 None."""
    migration = spec.database.migration
    if migration is not None and migration.change == "breaking":
        return "스키마를 옛 코드와 호환되지 않게 바꾼다 (database.migration.change: breaking)"
    if previous is None or previous.database.engine == "none":
        return None
    old, new = previous.database, spec.database
    if (old.engine, old.placement) != (new.engine, new.placement):
        return f"DB 가 바뀐다 ({old.placement} {old.engine} → {new.placement} {new.engine}) — 두 버전이 다른 DB 에 쓴다"
    return None


STRATEGY_REASON: dict[Strategy, str] = {
    "canary": "두 버전이 함께 돌아도 되는 변경이라 단계 배포(canary)",
    "bluegreen": "두 버전이 함께 돌면 안 되는 변경이라 미리보기 확인 뒤 한 번에 전환(bluegreen)",
}


def choose_strategy(spec: AppSpec, previous: AppSpec | None) -> Strategy:
    return "bluegreen" if mixed_versions_unsafe(spec, previous) else "canary"
