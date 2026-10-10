"""데이터 이전 Job — 이전 배포의 SQLite(볼륨) 데이터를 Postgres 로 한 번 옮긴다 (설계 '앱 분석/변환' ⑤).

PreSync hook, sync-wave 1 — 같은 동기화에서 마이그레이션 Job(wave 0)이 테이블을 만든 뒤, 새 버전이 뜨기 전에 돈다.
  dump (initContainer, sqlite3): PVC 를 읽기 전용으로 붙이고 DB 파일(+ -wal·-shm)을 emptyDir 로 복사해 연다.
        읽기 전용 볼륨에서 바로 열면 아직 체크포인트되지 않은 WAL 내용을 놓친다. 테이블마다 헤더 있는 CSV 로 덤프
  load (postgres psql): data_import_log 에 이 출처가 있으면 건너뛴다(한 번만). 없으면 한 트랜잭션에서
        표식 기록 → Postgres 에 있는 테이블만 외래 키 순서로 \\copy → id 시퀀스를 max(id)+1 로.
        CSV 가 있는데 옮긴 테이블이 0 이면 실패. 하나라도 실패하면 트랜잭션째 되돌려 표식도 남지 않는다
Postgres 에 없는 SQLite 테이블(옛 세션 저장소 등)은 건너뛴다. 테이블·컬럼 이름은 [A-Za-z0-9_] 만 받는다(셸·SQL 에 들어간다).
옮기는 동안 옛 버전이 SQLite 에 쓴 것은 옮겨지지 않는다 — 엔진 변경은 DB-001 이 사람 승인으로 넘긴다.
"""

from __future__ import annotations

from typing import Any

from review_ai.overlay.workload import pvc_name, same_node_term, secret_name
from review_ai.spec.deploy_spec import AppSpec

DUMP_IMAGE = "keinos/sqlite3:3.46.1"
LOAD_IMAGE = "postgres:16-alpine"
IMPORT_DEADLINE_SECONDS = 600
SAME_NODE_WEIGHT = 100
SOURCE_DIR, WORK_DIR = "/source", "/work"
POSTGRES_UID = 70  # postgres:alpine 의 postgres 사용자

# $1 = 파일 이름
DUMP_SCRIPT = r"""set -eu
test -f "/source/$1" || { echo "SQLite 파일이 없다: $1"; exit 1; }
cp /source/"$1"* /work/
db="/work/$1"
for t in $(sqlite3 "$db" "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"); do
  case "$t" in *[!A-Za-z0-9_]*) echo "건너뜀(이름): $t"; continue;; esac
  sqlite3 -csv -header "$db" "SELECT * FROM \"$t\"" > "/work/$t.csv"
  echo "dump $t $(($(wc -l < "/work/$t.csv") - 1))행"
done
rm -f "$db" "$db-wal" "$db-shm"
"""

# $1 = 출처 표식 (sqlite:<앱>/<볼륨>/<파일>). DATABASE_URL 은 앱과 같은 Secret
LOAD_SCRIPT = r"""set -eu
q() { psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -tAq -c "$1"; }
q "CREATE TABLE IF NOT EXISTS data_import_log (source TEXT PRIMARY KEY, imported_at TIMESTAMPTZ NOT NULL DEFAULT now())"
if [ "$(q "SELECT count(*) FROM data_import_log WHERE source = '$1'")" != 0 ]; then echo "이미 옮겼다: $1"; exit 0; fi
sql=/work/import.sql
printf '\\o /dev/null\nBEGIN;\nINSERT INTO data_import_log (source) VALUES (%s);\n' "'$1'" > "$sql"
found=0; loaded=0
for f in /work/*.csv; do [ -s "$f" ] && found=$((found + 1)); done
# 외래 키가 가리키는 테이블부터 — 깊이(참조 사슬 길이)가 작은 순서
order=$(q "WITH RECURSIVE fk AS (SELECT conrelid::regclass::text AS t, confrelid::regclass::text AS ref
  FROM pg_constraint WHERE contype = 'f' AND conrelid <> confrelid),
  lvl(t, depth) AS (SELECT relname::text, 0 FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'
  UNION ALL SELECT fk.t, lvl.depth + 1 FROM fk JOIN lvl ON fk.ref = lvl.t WHERE lvl.depth < 32)
  SELECT t FROM lvl GROUP BY t ORDER BY max(depth), t" | tr '\n' ' ')
for f in /work/*.csv; do
  t=$(basename "$f" .csv)
  case " $order " in *" $t "*) ;; *) [ -s "$f" ] && echo "건너뜀(Postgres 에 없음): $t";; esac
done
for t in $order; do
  f="/work/$t.csv"
  [ -s "$f" ] || continue
  cols=$(head -1 "$f" | tr -d '\r')
  case "$cols" in *[!A-Za-z0-9_,]*|'') echo "건너뜀(컬럼 이름): $t"; continue;; esac
  loaded=$((loaded + 1))
  echo "\\copy $t ($cols) FROM '$f' WITH (FORMAT csv, HEADER true)" >> "$sql"
  case ",$cols," in *,id,*)
    echo "SELECT setval(pg_get_serial_sequence('$t', 'id'), COALESCE(max(id), 0) + 1, false) FROM $t WHERE pg_get_serial_sequence('$t', 'id') IS NOT NULL;" >> "$sql";;
  esac
done
if [ "$found" -gt 0 ] && [ "$loaded" -eq 0 ]; then echo "옮길 수 있는 테이블이 없다 — 마이그레이션이 테이블을 만들었는지 확인"; exit 1; fi
echo 'COMMIT;' >> "$sql"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$sql"
echo "옮김: 테이블 $loaded 개 ($1)"
"""

_RESTRICTED = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
_SMALL = {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}}


def data_import_job_name(spec: AppSpec) -> str:
    return f"{spec.metadata.name}-data-import"


def import_source(spec: AppSpec) -> str:
    data_import = spec.database.data_import
    assert data_import is not None
    return f"sqlite:{spec.metadata.name}/{data_import.from_volume}/{data_import.file}"


def data_import_job(spec: AppSpec) -> dict[str, Any] | None:
    data_import = spec.database.data_import
    if data_import is None:
        return None
    database_url = {"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {
        "name": secret_name(spec), "key": spec.database.env_var}}}
    dump = {
        "name": "dump", "image": DUMP_IMAGE, "command": ["sh", "-c", DUMP_SCRIPT, "dump", data_import.file],
        "volumeMounts": [{"name": "source", "mountPath": SOURCE_DIR, "readOnly": True},
                         {"name": "work", "mountPath": WORK_DIR}],
        "resources": _SMALL, "securityContext": {**_RESTRICTED, "runAsNonRoot": True},
    }
    load = {
        "name": "load", "image": LOAD_IMAGE, "command": ["sh", "-c", LOAD_SCRIPT, "load", import_source(spec)],
        "env": [database_url],
        "volumeMounts": [{"name": "work", "mountPath": WORK_DIR}],
        "resources": _SMALL, "securityContext": {**_RESTRICTED, "runAsNonRoot": True, "runAsUser": POSTGRES_UID},
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": data_import_job_name(spec),
            "annotations": {"argocd.argoproj.io/hook": "PreSync",
                            "argocd.argoproj.io/sync-wave": "1",  # 마이그레이션(wave 0)이 테이블을 만든 다음
                            "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation"},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": IMPORT_DEADLINE_SECONDS,
            "template": {"spec": {
                "restartPolicy": "Never",
                # PreSync 라 옛 앱 Pod 가 아직 PVC 를 붙이고 있다 — 다른 노드에 뜨면 Multi-Attach 로 못 붙는다.
                # required 면 앱 Pod 가 없을 때(내려가 있을 때) 영원히 Pending 이라 preferred 로 둔다.
                "affinity": {"podAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
                    {"weight": SAME_NODE_WEIGHT, "podAffinityTerm": same_node_term(spec)}]}},
                "initContainers": [dump],
                "containers": [load],
                "volumes": [
                    {"name": "source", "persistentVolumeClaim": {
                        "claimName": pvc_name(spec, data_import.from_volume), "readOnly": True}},
                    {"name": "work", "emptyDir": {"sizeLimit": "1Gi"}},
                ],
            }},
        },
    }
