"""Rollout 컨테이너·볼륨·PVC — gitops base 의 Rollout 컨테이너 0번을 통째로 바꾸는 JSON6902 op 를 만든다.

컨테이너를 필드 단위가 아니라 통째로 바꾸므로 base 에 무엇이 있든 결과가 deploy_spec 하나로 정해진다.
"""

from __future__ import annotations

from typing import Any

from review_ai.spec.deploy_spec import AppSpec

SECRET_NAME_SUFFIX = "-secrets"
READINESS_PERIOD = 5
LIVENESS_PERIOD = 10


def secret_name(spec: AppSpec) -> str:
    return f"{spec.metadata.name}{SECRET_NAME_SUFFIX}"


def pvc_name(spec: AppSpec, volume: str) -> str:
    return f"{spec.metadata.name}-{volume}"


def _probe(path: str, port: int, period: int) -> dict[str, Any]:
    return {"httpGet": {"path": path, "port": port}, "periodSeconds": period}


def container_env(spec: AppSpec) -> list[dict[str, Any]]:
    plain = [{"name": k, "value": v} for k, v in spec.runtime.env.items()]
    refs = [
        {"name": s.name, "valueFrom": {"secretKeyRef": {"name": secret_name(spec), "key": s.name}}}
        for s in spec.secrets
    ]
    return plain + refs


def resources(spec: AppSpec) -> dict[str, Any]:
    r = spec.runtime.resources
    out: dict[str, Any] = {"requests": {"cpu": r.cpu_request, "memory": r.memory_request}}
    limits = {k: v for k, v in (("cpu", r.cpu_limit), ("memory", r.memory_limit)) if v}
    if limits:
        out["limits"] = limits
    return out


def container(spec: AppSpec) -> dict[str, Any]:
    rt = spec.runtime
    c: dict[str, Any] = {
        "name": spec.metadata.name,  # image 는 넣지 않는다 — rollout_ops 가 base 값(CI 가 태그를 쓴 값)을 복사한다
        "ports": [{"containerPort": rt.port}],
    }
    env = container_env(spec)
    if env:
        c["env"] = env
    if rt.health.readiness:
        c["readinessProbe"] = _probe(rt.health.readiness, rt.port, READINESS_PERIOD)
    if rt.health.liveness:
        c["livenessProbe"] = _probe(rt.health.liveness, rt.port, LIVENESS_PERIOD)
    c["resources"] = resources(spec)
    if spec.storage.volumes:
        c["volumeMounts"] = [{"name": v.name, "mountPath": v.mount_path} for v in spec.storage.volumes]
    return c


def pod_volumes(spec: AppSpec) -> list[dict[str, Any]]:
    return [
        {"name": v.name, "persistentVolumeClaim": {"claimName": pvc_name(spec, v.name)}}
        if v.persistent
        else {"name": v.name, "emptyDir": {"sizeLimit": v.size}}
        for v in spec.storage.volumes
    ]


def pvc(spec: AppSpec, volume_name: str) -> dict[str, Any]:
    v = next(v for v in spec.storage.volumes if v.name == volume_name)
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": pvc_name(spec, v.name)},
        "spec": {"accessModes": [v.access_mode], "resources": {"requests": {"storage": v.size}}},
    }


CONTAINERS = "/spec/template/spec/containers"


def rollout_ops(spec: AppSpec) -> list[dict[str, Any]]:
    """컨테이너 0번을 통째로 바꾸되 image 는 base 것을 그대로 쓴다.

    이미지 태그는 CI 가 base kustomization 의 newTag 로 갱신한다. 그 값은 base 빌드 때 이미 붙어 있으므로
    새 컨테이너를 1번에 넣고 → 0번의 image 를 복사한 뒤 → 옛 0번을 지운다 (base 컨테이너는 하나라는 전제).
    """
    ops: list[dict[str, Any]] = [
        {"op": "replace", "path": "/spec/replicas", "value": spec.runtime.replicas},
        {"op": "add", "path": f"{CONTAINERS}/-", "value": container(spec)},
        {"op": "copy", "from": f"{CONTAINERS}/0/image", "path": f"{CONTAINERS}/1/image"},
        {"op": "remove", "path": f"{CONTAINERS}/0"},
        {"op": "add", "path": "/spec/template/spec/terminationGracePeriodSeconds",
         "value": spec.runtime.termination_grace_seconds},
    ]
    volumes = pod_volumes(spec)
    if volumes:
        ops.append({"op": "add", "path": "/spec/template/spec/volumes", "value": volumes})
    return ops


def service_ops(spec: AppSpec) -> list[dict[str, Any]]:
    return [{"op": "replace", "path": "/spec/ports/0/targetPort", "value": spec.runtime.port}]
