"""배포 계획 렌더링 — bluegreen 전략, 마이그레이션 Job, 스모크 Job (실행 시점은 review_ai.deploy_plan).

- canary 는 base Rollout 의 strategy 를 그대로 둔다 (단계·분석은 gitops base 담당).
- bluegreen 은 strategy 를 통째로 바꾸고 미리보기 Service 를 만든다. 두 Service 의 selector 는 Rollouts 가 관리한다.
- 마이그레이션 Job 은 Argo CD hook 이다. PreSync 가 실패하면 동기화가 멈춰 새 버전이 뜨지 않는다.
  이미지는 앱과 같은 버전이어야 하므로 kustomize replacements 로 Rollout 컨테이너 이미지(CI 가 태그를 쓴 값)를 복사한다.
- 스모크 Job 은 PostSync hook — Rollout 이 Healthy 가 된 뒤 Service 로 smoke.paths 를 GET 한다.
  실패하면 동기화가 실패로 끝나 Argo CD 알림(on-sync-failed)으로 이어진다. 경로는 인자로 넘겨 셸에 끼워 넣지 않는다.
"""

from __future__ import annotations

from typing import Any

from review_ai.deploy_plan import migration_hook
from review_ai.overlay.workload import container_env, resources
from review_ai.spec.deploy_spec import AppSpec

SERVICE_PORT = 80  # gitops base service.yaml 과 같은 값
SCALE_DOWN_DELAY_SECONDS = 30  # 전환 뒤 옛 버전을 남겨 두는 시간 — 그 안에는 Service 만 되돌리면 된다
MIGRATION_DEADLINE_SECONDS = 300
SMOKE_IMAGE = "curlimages/curl:8.11.1"
SMOKE_DEADLINE_SECONDS = 120
# 경로마다 5번까지 다시 시도한다 — 전환 직후 Service 엔드포인트가 바뀌는 동안의 일시 오류는 실패로 보지 않는다
SMOKE_SCRIPT = (
    'for url in "$@"; do '
    'curl -fsS -o /dev/null --retry 5 --retry-delay 3 --retry-all-errors --max-time 5 "$url" '
    '|| { echo "FAIL $url"; exit 1; }; echo "ok $url"; done'
)


def preview_service_name(spec: AppSpec) -> str:
    return f"{spec.metadata.name}-preview"


def migration_job_name(spec: AppSpec) -> str:
    return f"{spec.metadata.name}-migrate"


def strategy_ops(spec: AppSpec) -> list[dict[str, Any]]:
    if spec.rollout.strategy == "canary":
        return []
    blue_green = {
        "activeService": spec.metadata.name,
        "previewService": preview_service_name(spec),
        "autoPromotionEnabled": True,  # readiness 를 통과하면 전환한다. 실패하면 progressDeadlineAbort 로 중단된다
        "scaleDownDelaySeconds": SCALE_DOWN_DELAY_SECONDS,
    }
    return [{"op": "replace", "path": "/spec/strategy", "value": {"blueGreen": blue_green}}]


def preview_service(spec: AppSpec) -> dict[str, Any]:
    name = spec.metadata.name
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": preview_service_name(spec)},
        "spec": {"selector": {"app": name},
                 "ports": [{"port": SERVICE_PORT, "targetPort": spec.runtime.port}]},
    }


def migration_job(spec: AppSpec) -> dict[str, Any] | None:
    migration = spec.database.migration
    if migration is None:
        return None
    hook = migration_hook(migration.change)
    container: dict[str, Any] = {
        "name": "migrate",
        "image": spec.image.repository,  # replacements 가 Rollout 이미지(태그 포함)로 바꾼다
        "command": list(migration.command),
        "resources": resources(spec),
    }
    env = container_env(spec)
    if env:
        container["env"] = env
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": migration_job_name(spec),
            "annotations": {
                "argocd.argoproj.io/hook": hook,
                # 다음 동기화 때 지우고 다시 만든다 — 실패한 Job 의 로그가 그때까지 남는다
                "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation",
                "review.crystal/schema-change": migration.change,
            },
        },
        "spec": {
            "backoffLimit": 0,  # 마이그레이션은 재시도하지 않는다 — 반쯤 적용된 상태에서 다시 돌리면 결과를 알 수 없다
            "activeDeadlineSeconds": MIGRATION_DEADLINE_SECONDS,
            "template": {"spec": {"restartPolicy": "Never", "containers": [container]}},
        },
    }


def image_replacements(spec: AppSpec) -> list[dict[str, Any]]:
    if spec.database.migration is None:
        return []
    return [{
        "source": {"kind": "Rollout", "name": spec.metadata.name,
                   "fieldPath": "spec.template.spec.containers.0.image"},
        "targets": [{"select": {"kind": "Job", "name": migration_job_name(spec)},
                     "fieldPaths": ["spec.template.spec.containers.0.image"]}],
    }]


def smoke_job_name(spec: AppSpec) -> str:
    return f"{spec.metadata.name}-smoke"


def smoke_job(spec: AppSpec) -> dict[str, Any] | None:
    if spec.smoke is None:
        return None
    base = f"http://{spec.metadata.name}:{SERVICE_PORT}"
    container = {
        "name": "smoke",
        "image": SMOKE_IMAGE,
        "command": ["sh", "-c", SMOKE_SCRIPT, "smoke", *(f"{base}{path}" for path in spec.smoke.paths)],
        "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "32Mi"}},
        "securityContext": {"runAsNonRoot": True, "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": smoke_job_name(spec),
            "annotations": {"argocd.argoproj.io/hook": "PostSync",
                            "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation"},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": SMOKE_DEADLINE_SECONDS,
            "template": {"spec": {"restartPolicy": "Never", "containers": [container]}},
        },
    }
