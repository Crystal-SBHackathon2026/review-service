"""빈 명세를 권장값으로 만들고 기존 정적 검사·RAG·교정 그래프로 검토한다.

파일 조회·저장·커밋은 파이프라인 몫이다. 이 모듈은 네트워크나 Git 쓰기를 하지 않는다.
GenerationContext 는 파이프라인이 확인한 앱/빌드 설정, baseline 은 업무 DB 의 승인 명세다.
추정값도 명세로 반환하지만 verification 이 남으면 커밋 가능하다고 표시하지 않는다.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from review_ai.catalog import load_targets
from review_ai.deploy_plan import STRATEGY_REASON, choose_strategy
from review_ai.graph import initial_state, run_graph
from review_ai.judge.llm import LlmClient
from review_ai.masking import mask_spec
from review_ai.overlay import render_overlay
from review_ai.overlay.yaml_io import dump
from review_ai.patching import apply_ops
from review_ai.retrieval import Retriever
from review_ai.secrets_pattern import MASK
from review_ai.spec.deploy_spec import (
    API_VERSION, BASE_RESOURCES, DNS_LABEL, REPOSITORY, AppSpec, Baseline, Database, DeploySpec, Image,
    Metadata, Network, Requirements, Rollout, Runtime, SecretRef, Smoke, Storage, Target,
)

PRESET = "container-http/v1"


class GenerationContext(BaseModel):
    """레포·대상은 필수. 선택 설정은 확인된 사실만 넣고, 모르는 것은 생략한다."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(pattern=REPOSITORY)
    target: Target
    name: str | None = Field(default=None, pattern=DNS_LABEL)
    image: Image | None = None
    runtime: Runtime | None = None
    requirements: Requirements | None = None
    database: Database | None = None
    secrets: tuple[SecretRef, ...] | None = None
    network: Network | None = None
    storage: Storage | None = None
    smoke: Smoke | None = None


@dataclass(frozen=True)
class Recommendation:
    path: str
    source: Literal["preset", "catalog", "baseline", "rule"]
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "source": self.source, "reason": self.reason}


@dataclass(frozen=True)
class Verification:
    """사람 질문이 아니라 파이프라인이 자동 검사/레포 분석으로 해소할 수 있는 작업."""

    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class PreparedSpec:
    spec: AppSpec
    origin: Literal["provided", "generated"]
    recommendations: tuple[Recommendation, ...] = ()
    verification: tuple[Verification, ...] = ()

    def to_response(self) -> dict[str, Any]:
        """외부 응답용: 평문 비밀을 가린다. 커밋에는 내부 spec 또는 원본을 쓴다."""
        spec = mask_spec(self.spec.model_dump(mode="json", exclude_none=True))
        return mask_spec({
            "origin": self.origin,
            "preset": PRESET if self.origin == "generated" else None,
            "deploy_spec": spec,
            "yaml": dump(spec),
            "recommendations": [r.as_dict() for r in self.recommendations],
            "verification": [v.as_dict() for v in self.verification],
        })


def app_name(repository: str) -> str:
    """레포 이름 → DNS 라벨 앱 이름 (생성 명세 metadata.name 과 같은 값)."""
    original = repository.split("/", 1)[1].lower()
    name = re.sub(r"[^a-z0-9-]", "-", original).strip("-") or "app"
    if not name[0].isalpha():
        name = "app-" + name
    if name != original or len(name) > 39:
        name = name[:32].rstrip("-") + "-" + hashlib.sha256(repository.encode()).hexdigest()[:6]
    return name


def _read(raw: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(raw, str) and not raw.strip():
        return None
    loaded = yaml.safe_load(raw) if isinstance(raw, str) else copy.deepcopy(raw)
    # None·빈 문서·주석뿐인 파일·{}만 빈 입력이다. [], false, 0 등은 형식 오류다.
    if loaded is None or isinstance(loaded, dict) and not loaded:
        return None
    if not isinstance(loaded, dict):
        raise ValueError("배포 명세는 YAML 객체여야 한다")
    return loaded


def prepare_spec(
    raw: str | dict[str, Any] | None,
    *,
    context: GenerationContext,
    baseline: Baseline | None = None,
) -> PreparedSpec:
    """유효한 기존 명세는 보존, 빈 입력만 생성. 잘못된 기존 명세를 기본값으로 덮지 않는다."""
    loaded = _read(raw)
    if loaded is not None:
        spec = AppSpec.model_validate(loaded)
        if (spec.metadata.repository, spec.target.env) != (context.repository, context.target.env):
            raise ValueError("명세와 생성 문맥의 레포·대상 환경이 다르다")
        return PreparedSpec(spec=spec, origin="provided")

    previous = baseline.spec if baseline else None
    if previous and (previous.metadata.repository, previous.target.env) != (context.repository, context.target.env):
        raise ValueError("baseline 은 같은 레포·대상 환경의 승인 명세여야 한다")
    if previous and context.name and previous.metadata.name != context.name:
        raise ValueError("baseline 과 앱 이름이 다르다")

    caps = load_targets()[context.target.env]
    recommendations: list[Recommendation] = []
    verification: list[Verification] = []
    name = context.name or (previous.metadata.name if previous else app_name(context.repository))
    values: dict[str, Any] = {}
    defaults: dict[str, Any] = {
        "image": Image(repository=f"ghcr.io/{context.repository.lower()}", platforms=tuple(sorted(caps.arch))),
        "runtime": Runtime(port=8080, resources=BASE_RESOURCES),  # 존재하지 않는 readiness 경로를 만들어 내지 않는다
        "requirements": Requirements(), "database": Database(), "secrets": (),
        "network": Network(), "storage": Storage(),
    }
    reasons = {
        "image": "GHCR 경로와 대상 아키텍처를 빌드 후보로 제안한다. 실제 이미지 정보를 확인해야 한다",
        "runtime": "HTTP 시작 프리셋: port 8080, replicas 1, CPU 50m/상한 250m, memory 64Mi/상한 128Mi. probe 경로는 추측하지 않는다",
        "requirements": "영속성 미선언의 후보값이다. 데이터 보존 불필요를 확인한 사실은 아니다",
        "database": "DB 미선언의 후보값이다. 앱이 DB 를 쓰지 않는다는 관측은 아니다",
        "secrets": "시크릿 값은 생성하거나 추측하지 않는다. 앱이 요구하는 참조를 확인해야 한다",
        "network": "기본값은 클러스터 내부만 접근(외부 노출 없음). 외부에 공개하려면 deploy.yaml 에 network.ingress 를 넣는다",
        "storage": "기본 후보에는 볼륨·버킷을 추가하지 않는다. 파일 저장 요구를 확인해야 한다",
    }
    for field, default in defaults.items():
        supplied = getattr(context, field)
        if supplied is not None:
            values[field] = supplied
        elif previous is not None:
            values[field] = getattr(previous, field)
            recommendations.append(Recommendation(f"/{field}", "baseline", "이전 승인 명세의 설정을 보존한다"))
        else:
            values[field] = default
            recommendations.append(Recommendation(f"/{field}", "catalog" if field == "image" else "preset", reasons[field]))

    if previous is None:
        checks = {
            "image": ("IMAGE_UNVERIFIED", "실제 이미지 경로·빌드 플랫폼을 확인해 context.image 에 넣는다"),
            "runtime": ("RUNTIME_UNVERIFIED", "실행 포트·실제 readiness 경로를 확인해 context.runtime 에 넣는다"),
            "requirements": ("PERSISTENCE_UNVERIFIED", "앱의 재배포 후 데이터 보존 요구를 확인한다"),
            "database": ("DATABASE_UNVERIFIED", "앱의 DB 사용 여부·엔진·배치를 확인한다"),
            "secrets": ("SECRETS_UNVERIFIED", "앱에서 필요한 시크릿 참조 목록을 확인한다"),
            "storage": ("STORAGE_UNVERIFIED", "파일·볼륨·버킷 저장 요구를 확인한다"),
        }
        verification.extend(Verification(code, f"/{field}", message)
                            for field, (code, message) in checks.items() if getattr(context, field) is None)
    # 확인 경로는 선택 항목이라 확인 항목(verification)이 아니다 — 근거가 있을 때만 넣는다
    smoke = context.smoke or (previous.smoke if previous else None)
    spec = AppSpec(api_version=API_VERSION, kind="DeploySpec",
                   metadata=Metadata(name=name, repository=context.repository), target=context.target,
                   smoke=smoke, **values)
    strategy = choose_strategy(spec, previous)
    spec = spec.model_copy(update={"rollout": Rollout(strategy=strategy)})
    recommendations.append(Recommendation("/rollout", "rule", STRATEGY_REASON[strategy]))
    return PreparedSpec(spec=spec, origin="generated", recommendations=tuple(recommendations),
                        verification=tuple(verification))


@dataclass(frozen=True)
class PreparedReview:
    prepared: PreparedSpec
    review: dict[str, Any]
    render_warnings: tuple[dict[str, Any], ...]

    @property
    def ready_to_commit(self) -> bool:
        return (self.review["status"] == "pass" and not self.prepared.verification
                and not any(w["blocking"] for w in self.render_warnings))

    def to_response(self) -> dict[str, Any]:
        return mask_spec({**self.prepared.to_response(), "review": self.review,
                          "render_warnings": list(self.render_warnings), "ready_to_commit": self.ready_to_commit})


async def prepare_and_review(
    raw: str | dict[str, Any] | None,
    *,
    context: GenerationContext,
    review_id: str,
    llm: LlmClient | None,
    retriever: Retriever,
    baseline: Baseline | None = None,
    spec_ref: dict[str, str] | None = None,
    autofix_commit: bool = False,
) -> PreparedReview:
    """생성 → static_check → RAG → judge → 허용된 교정·재검사 → 최종 명세 반환.

    별도 승인 단계를 강제하지 않는다. transient 에러는 기존 RetryPolicy 를 위해 그대로 올린다.
    ready_to_commit 은 검토·생성 확인·렌더러 게이트를 합친 값이며 Git 쓰기를 수행하지 않는다.
    """
    prepared = prepare_spec(raw, context=context, baseline=baseline)
    original = prepared.spec.model_dump(mode="json")
    validated = DeploySpec.model_validate({**original, "baseline": baseline})
    state = initial_state(mask_spec(validated.model_dump(mode="json")), review_id=review_id,
                          spec_ref=spec_ref, autofix_commit=autofix_commit)
    final = await run_graph(state, llm=llm, retriever=retriever)
    # graph 의 사본은 가려져 있다. 최종 ops 를 원본에 적용해야 비밀이 덮어써지지 않는다.
    fixed = AppSpec.model_validate(apply_ops(original, final["applied_ops"]))
    prepared = replace(prepared, spec=fixed)
    warnings: tuple[dict[str, Any], ...] = ()
    if MASK in fixed.model_dump_json():
        prepared = replace(prepared, verification=(*prepared.verification,
            Verification("ORIGINAL_SPEC_REQUIRED", "/", "가린 사본 대신 원본 명세로 렌더링해야 한다")))
    else:
        warnings = tuple(w.to_dict() for w in render_overlay(fixed).warnings)
    return PreparedReview(prepared=prepared, review=final, render_warnings=warnings)
