"""환경별 Ingress. 클래스·고정 annotation 은 catalog/targets.yaml, 공개 여부·TLS·대역은 deploy_spec 에서."""

from __future__ import annotations

import json
from typing import Any

from review_ai.catalog import TargetCaps
from review_ai.spec.deploy_spec import AppSpec, Ingress

SERVICE_PORT = 80


def _aws_annotations(spec: AppSpec, ingress: Ingress) -> dict[str, str]:
    listen: list[dict[str, int]] = [{"HTTP": 80}] + ([{"HTTPS": 443}] if ingress.tls else [])
    out = {
        "alb.ingress.kubernetes.io/scheme": "internet-facing" if ingress.public else "internal",
        "alb.ingress.kubernetes.io/target-type": "ip",
        "alb.ingress.kubernetes.io/healthcheck-path": spec.runtime.health.readiness or "/",
        "alb.ingress.kubernetes.io/listen-ports": json.dumps(listen),
    }
    if ingress.tls:
        out["alb.ingress.kubernetes.io/ssl-redirect"] = "443"  # 인증서는 host 로 ACM 에서 자동 탐색
    if ingress.allowed_cidrs:
        out["alb.ingress.kubernetes.io/inbound-cidrs"] = ",".join(ingress.allowed_cidrs)
    return out


def _class(caps: TargetCaps, ingress: Ingress) -> str:
    if caps.env == "gcp" and not ingress.public:
        return "gce-internal"
    return caps.ingress_class


def render_ingress(spec: AppSpec, caps: TargetCaps) -> tuple[dict[str, Any], list[str]]:
    ingress = spec.network.ingress
    if ingress is None:
        raise ValueError("network.ingress 가 없는 명세는 Ingress 를 만들지 않는다")
    name = spec.metadata.name
    warnings: list[str] = []
    annotations = _aws_annotations(spec, ingress) if caps.env == "aws" else {}
    annotations.update(caps.ingress_annotations)
    if ingress.allowed_cidrs and caps.env != "aws":
        warnings.append(f"{caps.env}: network.ingress.allowed_cidrs 는 overlay 로 강제하지 못한다 (Traefik middleware·Cloud Armor 필요)")
    rule: dict[str, Any] = {"http": {"paths": [{
        "path": "/", "pathType": "Prefix",
        "backend": {"service": {"name": name, "port": {"number": SERVICE_PORT}}},
    }]}}
    if ingress.host:
        rule = {"host": ingress.host, **rule}
    body: dict[str, Any] = {"ingressClassName": _class(caps, ingress), "rules": [rule]}
    if ingress.tls:
        if not ingress.host:
            warnings.append("network.ingress.tls 인데 host 가 없다 — 인증서를 붙일 수 없다")
        elif caps.env != "aws":
            body["tls"] = [{"hosts": [ingress.host], "secretName": f"{name}-tls"}]
            warnings.append(f"TLS 인증서 Secret {name}-tls 가 미리 있어야 한다 (cert-manager 등)")
    metadata: dict[str, Any] = {"name": name}
    if annotations:
        metadata["annotations"] = annotations
    doc = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": metadata, "spec": body}
    return doc, warnings
