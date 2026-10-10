"""deploy_spec → gitops kustomize overlay 렌더러 (결정 #7: 검토 서비스 코드가 overlay 를 만든다).

- 입력은 deploy_spec 하나, 출력은 apps/<app>/overlays/<env>/ 아래 파일 내용 (dict). 디스크에 쓰지 않는다.
  gitops 레포에 커밋하는 일은 파이프라인 커밋 단계가 한다.
- 전제: apps/<app>/base 에 Rollout·Service(이름 = metadata.name)가 있다 — 지금 sample-app base 구조.
- overlay 로 표현할 수 없는 것(DB·버킷 프로비저닝, Secret 값 생성)은 warnings 로 돌려준다. 조용히 빼지 않는다.
  경고마다 code·blocking 이 있다(overlay/warnings.py). blocking 이 하나라도 있으면 커밋하지 말고 멈춘다 — RenderedOverlay.blocking.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from review_ai.catalog import TargetCaps, load_targets
from review_ai.overlay.data_import import data_import_job
from review_ai.overlay.ingress import render_ingress
from review_ai.overlay.plan import image_replacements, migration_job, preview_service, smoke_job, strategy_ops
from review_ai.overlay.warnings import RenderWarning
from review_ai.overlay.workload import pvc, rollout_ops, secret_name, service_ops
from review_ai.overlay.yaml_io import dump, dump_with_header
from review_ai.secrets_pattern import MASK
from review_ai.spec.deploy_spec import AppSpec

HEADER = (
    "생성 파일 — review-service overlay 렌더러가 deploy_spec 에서 만든다.\n"
    "손으로 고치지 말고 앱 레포의 deploy.yaml 을 고친다."
)


@dataclass(frozen=True)
class RenderedOverlay:
    directory: str
    files: dict[str, str]  # 파일 이름 → 내용
    warnings: tuple[RenderWarning, ...]

    @property
    def blocking(self) -> tuple[RenderWarning, ...]:
        """커밋하면 배포가 깨지거나 명세가 요구한 보호가 빠지는 경고. 비어 있어야 커밋한다."""
        return tuple(w for w in self.warnings if w.blocking)


def _warnings(spec: AppSpec, caps: TargetCaps) -> list[RenderWarning]:
    out = []
    if spec.secrets:
        names = ", ".join(s.name for s in spec.secrets)
        out.append(RenderWarning(
            "SECRET_KEYS_REQUIRED",
            f"Secret {secret_name(spec)} 에 키 [{names}] 가 미리 있어야 한다 (값 생성·동기화 주체는 결정 #8)",
        ))
    if spec.database.engine != "none" and spec.database.placement != "volume":
        out.append(RenderWarning(
            "DB_PROVISIONING_REQUIRED",
            f"database ({spec.database.placement} {spec.database.engine}) 는 overlay 로 만들지 않는다 — 인프라 프로비저닝 필요",
        ))
    if spec.storage.buckets:
        out.append(RenderWarning(
            "BUCKET_PROVISIONING_REQUIRED",
            "storage.buckets 는 overlay 로 만들지 않는다 — Terraform 등 인프라 쪽에서 반영 필요",
        ))
    for v in spec.storage.volumes:
        if v.persistent and v.access_mode not in caps.volume_access_modes:
            supported = ", ".join(sorted(caps.volume_access_modes)) or "없음"
            out.append(RenderWarning(
                "VOLUME_UNSUPPORTED",
                f"{caps.env}: 볼륨 {v.name} ({v.access_mode}) 를 만들 수 없다 — 지원 접근 모드: {supported} (STO-001)",
            ))
    retained = [f"{v.name} {v.size}" for v in spec.storage.volumes if v.persistent]
    if retained and caps.volume_reclaim_policy == "Retain":
        out.append(RenderWarning(
            "VOLUME_RETAINED",
            f"{caps.env}: PVC [{', '.join(retained)}] 는 {caps.storage_class or '기본 클래스'}(Retain) — "
            "PVC 를 지워도 PV·디스크와 과금이 남는다. 정리는 사람이 한다",
        ))
    return out


def render_overlay(spec: AppSpec, *, apps_root: str = "apps") -> RenderedOverlay:
    """spec 은 앱 레포에서 spec_ref 로 읽은 원본이어야 한다. Kafka 메시지의 가린 사본으로 렌더링하면 거절한다."""
    if MASK in spec.model_dump_json():
        raise ValueError("가린 명세(***MASKED***)로는 overlay 를 만들지 않는다 — spec_ref 로 원본을 읽어야 한다")
    caps = load_targets()[spec.target.env]
    name = spec.metadata.name
    files: dict[str, str] = {}
    warnings = _warnings(spec, caps)
    resources = ["../../base"]
    if spec.network.ingress is not None:
        ingress, ingress_warnings = render_ingress(spec, caps)
        files["ingress.yaml"] = dump_with_header(ingress, HEADER)
        warnings.extend(ingress_warnings)
        resources.append("ingress.yaml")
    for v in spec.storage.volumes:
        if v.persistent:
            filename = f"pvc-{v.name}.yaml"
            files[filename] = dump_with_header(pvc(spec, v.name, caps.storage_class), HEADER)
            resources.append(filename)
    if spec.rollout.strategy == "bluegreen":
        files["service-preview.yaml"] = dump_with_header(preview_service(spec), HEADER)
        resources.append("service-preview.yaml")
    jobs = (("job-migrate.yaml", migration_job(spec)), ("job-data-import.yaml", data_import_job(spec)),
            ("job-smoke.yaml", smoke_job(spec)))
    for filename, job in jobs:
        if job is not None:
            files[filename] = dump_with_header(job, HEADER)
            resources.append(filename)
    kustomization: dict[str, Any] = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": spec.target.namespace or name,
        "resources": resources,
        "patches": [
            {"target": {"kind": "Rollout", "name": name},
             "patch": dump(rollout_ops(spec) + strategy_ops(spec)).rstrip("\n")},
            {"target": {"kind": "Service", "name": name}, "patch": dump(service_ops(spec)).rstrip("\n")},
        ],
    }
    if spec.network.service is not None:
        # active Service의 이름·selector·clusterIP를 유지한다. 별도 Service를
        # 만들지 않으므로 Rollouts가 승격할 때 같은 공개 주소의 대상만 바뀐다.
        files["service-public.yaml"] = dump_with_header({
            "apiVersion": "v1", "kind": "Service", "metadata": {"name": name},
            "spec": {"type": "LoadBalancer"},
        }, HEADER)
        kustomization["patches"].append({"path": "service-public.yaml"})
    replacements = image_replacements(spec)
    if replacements:
        kustomization["replacements"] = replacements
    files["kustomization.yaml"] = dump_with_header(kustomization, HEADER)
    directory = f"{apps_root}/{name}/overlays/{spec.target.env}"
    return RenderedOverlay(directory=directory, files=dict(sorted(files.items())), warnings=tuple(warnings))


def overlay_diff(before: AppSpec, after: AppSpec) -> list[dict[str, str]]:
    """패치 전후 overlay 의 파일별 unified diff. Patch.files 에 들어간다."""
    old, new = render_overlay(before), render_overlay(after)
    diffs = []
    for filename in sorted(set(old.files) | set(new.files)):
        a, b = old.files.get(filename, ""), new.files.get(filename, "")
        if a == b:
            continue
        path = f"{new.directory}/{filename}"
        text = "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{path}", f"b/{path}"))
        diffs.append({"path": path, "diff": text})
    return diffs
