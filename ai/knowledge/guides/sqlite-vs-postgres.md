---
doc_type: guide
provider: any
title: SQLite 를 그대로 둘지 postgres 로 옮길지
related_rules: [DB-003, DB-005]
---
## 판단 기준
SQLite 는 replicas 1 + 영속 볼륨이면 운영 가능하다. "SQLite 면 무조건 postgres" 는 틀린 제안이다.
옮겨야 하는 경우는 replicas 를 늘려야 하거나(수평 확장), 여러 서비스가 같은 DB 를 써야 하거나, 관리형 백업이 필요할 때다.

## 옮길 때
엔진 변경은 데이터가 없을 때만 자동이고, 있으면 사람이 이전 계획(덤프·변환·검증)을 승인한다. 앱 드라이버·SQL 방언도 바뀐다.
