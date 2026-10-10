# 코드 패치 (앱 분석/변환 ④·⑤)

대상 환경에서 **그대로는 못 도는 새 앱**을 고치는 단계다. deploy.yaml 이 없는 PR(intake)에서 레포 분석 뒤에 돈다.

```
레포 분석(analyze) ─▶ plan_transform (코드, LLM 없음) ─▶ Claude (TransformOutput) ─▶ 코드 게이트 ─▶ apply_to_context
                                                                                    │
                    Review API: package-lock.json 재생성(npm) ─▶ 코드 + 잠금 파일 + deploy.yaml 한 커밋 ─▶ 일반 검토
```

무엇을 고칠지는 코드가 정하고(`review_ai/transform/plan.py`), LLM 은 그 항목만 코드로 옮긴다.
결과는 게이트(`gate.py`)가 원래 파일·계획과 대조하고, 하나라도 어기면 패치 전체를 버린다(`TRANSFORM_REJECTED`).
검토 서비스는 앱 코드를 실행하지 않는다 — 문법·동작은 그 커밋에 걸리는 앱 CI 가 확인한다.

## 계획 항목

| 항목 | 조건 | 패치 |
|---|---|---|
| `DB_SQLITE_TO_POSTGRES` | Node 앱이 SQLite 드라이버를 쓰는데 대상 환경이 SQLite 볼륨을 못 만든다 (지금 aws — PVC 는 되지만 SQLite 볼륨 배치는 열지 않음, DB-002) | `pg`·`DATABASE_URL`, `migrations/0001_init.sql`, `src/migrate.js`(schema_migrations 기록), 세션은 `connect-pg-simple`, 시작 코드의 CREATE TABLE 제거 |
| `METRICS_ENDPOINT` | Express 앱에 `/metrics` 가 없다 | `prom-client` 기본 지표 + 요청 수·처리 시간 |

DB 항목이 있으면 생성 명세가 이렇게 된다: `database: {engine: postgres, placement: managed, version: "16",
migration: {command: [node, src/migrate.js], change: expand}}`, 시크릿 `DATABASE_URL`(aws-secrets-manager `<앱>/database-url`),
`requirements.persistence: true`. 마이그레이션은 렌더러가 PreSync Job 으로 만든다(deploy-spec.md 배포 계획).

다른 언어(Python·Go)의 SQLite 는 자동 패치하지 않고 이유만 남긴다 — DB 는 후보값으로 커밋되고 그 PR 은 사람 확인(`unverified_paths`)이다.
다른 미해결 항목(포트·시크릿 등)이 남은 레포는 어차피 사람 확인으로 가므로 LLM 을 부르지 않는다.
코드 패치를 시도했는데 막혔으면(게이트·잠금 파일) 후보값으로 덮지 않고 그 사유로 거절한다.

## 게이트

| 코드 | 막는 것 |
|---|---|
| `PATH_NOT_ALLOWED` | 소스·package.json 밖(Dockerfile·CI·deploy.yaml·잠금 파일·레포 밖) 쓰기, 새 파일은 `src/*.js`·`migrations/*.sql` 만, 삭제는 SQLite 전용 파일만 |
| `SECRET_LITERAL`·`MASKED_VALUE` | 접속 문자열·토큰을 코드에 씀 / 프롬프트에서 가린 값을 옮김 |
| `DEPENDENCIES` | 계획 밖 패키지 추가·삭제, 기존 버전·devDependencies·scripts 변경(`migrate` 추가만 허용), `^x.y.z` 밖 버전 |
| `DRIVER_LEFT`·`ENV_MISSING` | SQLite 드라이버가 남음 / `DATABASE_URL` 로 접속하지 않음 |
| `ROUTES_REMOVED`·`METRICS_MISSING` | 기존 HTTP 경로(메서드·경로)가 사라짐 / `GET /metrics` 없음 |
| `MIGRATION_MISSING`·`STARTUP_DDL` | 원래 테이블·세션 테이블·migrate 스크립트가 없음 / 앱 코드에 CREATE TABLE 이 남음 |

## 잠금 파일

LLM 은 `package-lock.json` 을 쓸 수 없다(무결성 해시). 낡으면 앱 CI·Docker 빌드의 `npm ci` 가 실패한다.
Review API 가 빈 임시 디렉터리에 package.json·잠금 파일 두 개만 두고
`npm install --package-lock-only --ignore-scripts` 로 다시 만든다 (`api/review_api/lockfile.py`, API 이미지에 node 22).
설치·패키지 스크립트 실행 없이 레지스트리 메타데이터만 읽는다. 실패하거나 yarn·pnpm 잠금 파일이면 `LOCKFILE_UNAVAILABLE` 로 커밋하지 않는다.

## 실측 (10/09, sample-todo → aws)

- 정답 패치(`tests/fixtures/transform/sample-todo-patched`, 사람이 고친 `fixed-manual` 기반)와 **실제 Claude(Sonnet) 패치 둘 다**
  게이트 통과 → Postgres 16 컨테이너에서 마이그레이션 2회(멱등)·가입(201/중복 409)·로그인·할 일 저장/조회·비로그인 401·`/metrics` 확인
- Claude 1회: 입력 4k·출력 5k 토큰(≈$0.09), 파일 5개(server.js·migrate.js·0001_init.sql·package.json 변경, 세션 저장소 삭제)
- 직접 돌리기: `scripts/run_transform.py <레포 체크아웃> --env aws [--claude --out <디렉터리>]`
