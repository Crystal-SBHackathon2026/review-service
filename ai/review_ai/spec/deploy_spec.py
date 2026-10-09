"""deploy_spec 초안 (crystal.review/v1alpha1).

검토 서비스가 받는 배포 요청 명세. 여기서는 **형식**만 검사한다.
- 형식 오류(필드 누락·타입·정규식) → Review API 가 422 로 거절한다. finding 이 아니다.
- 의미 문제(SQLite 복제, 평문 시크릿, 엔진 변경 등) → static_check 가 finding 으로 낸다.
  그래서 의미 규칙으로 잡아야 할 조합은 여기서 막지 않는다.

`baseline` 은 사용자가 쓰지 않는다. 파이프라인이 업무 DB 에서 같은 앱·같은 환경의
마지막 승인 배포 명세와 관측 사실을 찾아 채운다. 첫 배포면 None.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

API_VERSION = "crystal.review/v1alpha1"

DNS_LABEL = r"^[a-z]([-a-z0-9]{0,38}[a-z0-9])?$"
ENV_NAME = r"^[A-Z_][A-Z0-9_]*$"
HTTP_PATH = r"^/[A-Za-z0-9._~/-]*$"
ABS_PATH = r"^/[A-Za-z0-9._/-]+$"
CPU_QUANTITY = r"^(\d+m|\d+(\.\d+)?)$"
SIZE_QUANTITY = r"^\d+(Mi|Gi|Ti)$"
COMMIT_SHA = r"^[0-9a-f]{7,40}$"
IMAGE_DIGEST = r"^sha256:[0-9a-f]{64}$"
REPOSITORY = r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$"

Provider = Literal["aws", "gcp", "local"]
Arch = Literal["amd64", "arm64"]
DbEngine = Literal["none", "postgres", "mysql", "sqlite"]
DbPlacement = Literal["managed", "in-cluster", "volume", "external"]
SecretSource = Literal["aws-secrets-manager", "gcp-secret-manager", "k8s-secret", "generated"]
AccessMode = Literal["ReadWriteOnce", "ReadWriteMany"]
Strategy = Literal["canary", "bluegreen"]
# 이 배포가 DB 스키마를 어떻게 바꾸는가 — 마이그레이션 실행 시점과 배포 전략이 여기서 정해진다 (review_ai.deploy_plan)
#   none      스키마 변경 없음
#   expand    추가만 (테이블·nullable 컬럼·인덱스) — 옛 코드가 새 스키마에서도 돈다 → 새 버전 전에 실행
#   contract  제거만 (옛 코드만 쓰던 컬럼·테이블) — 새 코드가 옛 스키마에서도 돈다 → 새 버전이 다 뜬 뒤 실행
#   breaking  이름 변경·타입 변경처럼 옛 코드와 새 코드가 같은 스키마에서 함께 돌 수 없다
SchemaChange = Literal["none", "expand", "contract", "breaking"]
MIGRATION_ENGINES = ("postgres", "mysql")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Metadata(_Frozen):
    name: str = Field(pattern=DNS_LABEL)
    repository: str = Field(pattern=REPOSITORY, description="owner/repo")
    commit: str | None = Field(
        default=None, pattern=COMMIT_SHA,
        description="앱 레포 deploy.yaml 은 자기 커밋을 모르니 비워 둔다. 검토한 커밋은 spec_ref.commit",
    )


class Target(_Frozen):
    env: Provider
    region: str = Field(min_length=1)
    namespace: str | None = Field(default=None, pattern=DNS_LABEL, description="생략하면 metadata.name")


class Image(_Frozen):
    """배포할 이미지 버전(태그)은 CI 가 gitops base 에 쓴다. 명세는 어떤 이미지·어떤 아키텍처인지만 말한다.

    병합 전 PR 에서는 병합 SHA 태그가 아직 없으므로 tag·digest 는 선택이다. overlay 렌더러는 둘 다 쓰지 않는다.
    """

    repository: str = Field(min_length=1)
    tag: str | None = Field(default=None, description="참고용. 배포 태그는 CI 가 정한다")
    digest: str | None = Field(default=None, pattern=IMAGE_DIGEST, description="참고용. 배포 태그는 CI 가 정한다")
    platforms: tuple[Arch, ...] = Field(min_length=1)


class Health(_Frozen):
    readiness: str | None = Field(default=None, pattern=HTTP_PATH)
    liveness: str | None = Field(default=None, pattern=HTTP_PATH)


class Resources(_Frozen):
    cpu_request: str = Field(default="50m", pattern=CPU_QUANTITY)
    memory_request: str = Field(default="64Mi", pattern=SIZE_QUANTITY)
    cpu_limit: str | None = Field(default=None, pattern=CPU_QUANTITY)
    memory_limit: str | None = Field(default=None, pattern=SIZE_QUANTITY)


# gitops base rollout.yaml 과 같은 값. overlay 는 컨테이너를 통째로 바꾸므로 상한을 비워 두면 상한 없이 배포된다(RUN-005).
# 생성한 명세와 RUN-005 권장값이 이 값을 쓴다
BASE_RESOURCES = Resources(cpu_limit="250m", memory_limit="128Mi")


class Runtime(_Frozen):
    port: int = Field(ge=1, le=65535)
    replicas: int = Field(default=1, ge=1, le=10)
    health: Health = Health()
    resources: Resources = Resources()
    env: dict[str, str] = Field(default_factory=dict, description="평문 값만. 비밀은 secrets 로")
    termination_grace_seconds: int = Field(default=30, ge=0, le=600)

    @model_validator(mode="after")
    def _env_names(self) -> Runtime:
        bad = [k for k in self.env if not re.fullmatch(ENV_NAME, k)]
        if bad:
            raise ValueError(f"runtime.env 이름은 대문자 환경변수 형식이어야 한다: {bad}")
        return self


class Requirements(_Frozen):
    persistence: bool = Field(default=False, description="재배포 뒤에도 데이터가 남아야 하는가")


class Migration(_Frozen):
    """배포 때 앱 이미지로 실행할 마이그레이션. 렌더러가 Argo CD hook Job 으로 만든다."""

    command: tuple[str, ...] = Field(min_length=1, description="앱 이미지 안에서 실행할 명령 (예: [npm, run, migrate])")
    change: SchemaChange = Field(description="이 배포의 스키마 변경 종류 — 실행 시점(PreSync·PostSync)과 배포 전략을 정한다")


class DataImport(_Frozen):
    """이전 배포가 볼륨의 SQLite 에 쌓은 데이터를 새 DB 로 한 번 옮긴다. 렌더러가 PreSync Job 으로 만든다(마이그레이션 다음).

    Postgres 의 data_import_log 에 표식을 남겨, 다음 동기화부터는 건너뛴다 — 옮긴 뒤 쌓인 데이터를 덮어쓰지 않는다.
    """

    from_volume: str = Field(pattern=DNS_LABEL, description="SQLite 파일이 있는 storage.volumes[].name (persistent)")
    file: str = Field(pattern=r"^[A-Za-z0-9._-]+$", max_length=100, description="볼륨 안 SQLite 파일 이름 (예: todo.db)")


class Database(_Frozen):
    engine: DbEngine = "none"
    version: str | None = Field(default=None, description="메이저 버전 (예: '16', '8.0')")
    placement: DbPlacement | None = None
    engine_policy: Literal["preserve", "allow_convert"] = Field(
        default="preserve", description="자동 수정이 엔진을 바꿔도 되는가 (데이터 없을 때만 적용)"
    )
    volume: str | None = Field(default=None, description="sqlite 일 때 storage.volumes[].name")
    env_var: str = Field(default="DATABASE_URL", pattern=ENV_NAME)
    backup_retention_days: int = Field(default=1, ge=0, le=35)
    publicly_accessible: bool = False
    migration: Migration | None = None
    data_import: DataImport | None = None

    @model_validator(mode="after")
    def _placement_when_engine(self) -> Database:
        if self.engine != "none" and self.placement is None:
            raise ValueError("database.engine 이 none 이 아니면 placement 가 필요하다")
        if self.migration is not None and self.engine not in MIGRATION_ENGINES:
            # SQLite 파일은 앱 파드의 볼륨 안에 있어 별도 Job 이 열 수 없다 — 앱이 시작할 때 마이그레이션한다
            raise ValueError(f"database.migration 은 {'·'.join(MIGRATION_ENGINES)} 에서만 쓴다 (지금 {self.engine})")
        if self.data_import is not None:
            # 테이블은 마이그레이션이 먼저 만든다 — 앱이 시작할 때 만드는 구조면 옮길 곳이 없어 데이터를 건너뛴다
            if self.engine != "postgres" or self.migration is None or self.migration.change == "contract":
                raise ValueError("database.data_import 는 postgres + 새 버전 전에 도는 migration(change ≠ contract)이 필요하다")
        return self


class SecretRef(_Frozen):
    """비밀 값 자체는 명세에 넣지 않는다. 어디서 읽을지만 적는다."""

    name: str = Field(pattern=ENV_NAME)
    source: SecretSource
    key: str | None = Field(default=None, description="source 안의 이름. generated 면 생략")

    @model_validator(mode="after")
    def _key_unless_generated(self) -> SecretRef:
        if self.source != "generated" and not self.key:
            raise ValueError(f"secrets[{self.name}]: source 가 generated 가 아니면 key 가 필요하다")
        return self


class Ingress(_Frozen):
    public: bool
    tls: bool = False
    host: str | None = None
    allowed_cidrs: tuple[str, ...] = Field(default=(), description="비우면 별도 제한 없음")

    @model_validator(mode="after")
    def _cidrs(self) -> Ingress:
        for cidr in self.allowed_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"network.ingress.allowed_cidrs 항목이 CIDR 이 아니다: {cidr!r}") from exc
        return self


class Network(_Frozen):
    ingress: Ingress | None = Field(default=None, description="None 이면 클러스터 밖으로 노출하지 않음")


class Volume(_Frozen):
    name: str = Field(pattern=DNS_LABEL)
    mount_path: str = Field(pattern=ABS_PATH)
    size: str = Field(pattern=SIZE_QUANTITY)
    persistent: bool = True
    access_mode: AccessMode = "ReadWriteOnce"


class Bucket(_Frozen):
    name: str = Field(min_length=3, max_length=63)
    public: bool = False
    versioning: bool = True
    encryption: bool = True


class Rollout(_Frozen):
    """canary: base Rollout 의 단계 배포를 그대로 쓴다. bluegreen: 새 버전을 미리보기 Service 로 띄워 확인한 뒤 한 번에 전환한다."""

    strategy: Strategy = "canary"


class Smoke(_Frozen):
    """배포가 끝난 뒤 클러스터 안에서 Service 로 GET 해 볼 경로. 하나라도 2xx·3xx 가 아니면 동기화가 실패로 끝난다."""

    paths: tuple[str, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _paths(self) -> Smoke:
        bad = [p for p in self.paths if not re.fullmatch(HTTP_PATH, p)]
        if bad:
            raise ValueError(f"smoke.paths 는 / 로 시작하는 경로여야 한다: {bad}")
        return self


class Storage(_Frozen):
    volumes: tuple[Volume, ...] = ()
    buckets: tuple[Bucket, ...] = ()


class AppSpec(_Frozen):
    """사용자가 선언하는 부분."""

    api_version: Literal["crystal.review/v1alpha1"]
    kind: Literal["DeploySpec"]
    metadata: Metadata
    target: Target
    image: Image
    runtime: Runtime
    requirements: Requirements = Requirements()
    database: Database = Database()
    secrets: tuple[SecretRef, ...] = ()
    network: Network = Network()
    storage: Storage = Storage()
    rollout: Rollout = Rollout()
    smoke: Smoke | None = Field(default=None, description="앱 레포에 테스트가 없으면 생성기가 확인 경로를 채운다")

    @model_validator(mode="after")
    def _import_volume(self) -> AppSpec:
        data_import = self.database.data_import
        if data_import is None:
            return self
        volume = next((v for v in self.storage.volumes if v.name == data_import.from_volume), None)
        if volume is None or not volume.persistent:
            raise ValueError(f"database.data_import.from_volume={data_import.from_volume!r} 는 "
                             "storage.volumes 의 persistent 볼륨이어야 한다 — 빼면 옮기기 전에 PVC 가 지워진다")
        return self


class BaselineFacts(_Frozen):
    """파이프라인이 관측한 사실. 모르면 None — 규칙은 None 을 '데이터 있음'으로 취급한다."""

    database_has_data: bool | None = None
    observed_at: datetime | None = None


class Baseline(_Frozen):
    spec_ref: str = Field(min_length=1, description="업무 DB 의 마지막 승인 배포 ID 또는 커밋")
    spec: AppSpec
    facts: BaselineFacts = BaselineFacts()


class DeploySpec(AppSpec):
    """State.deploy_spec 에 들어가는 전체. baseline 은 파이프라인이 채운다."""

    baseline: Baseline | None = None

    @model_validator(mode="after")
    def _baseline_same_app(self) -> DeploySpec:
        if self.baseline is None:
            return self
        prev = self.baseline.spec
        if (prev.metadata.name, prev.target.env) != (self.metadata.name, self.target.env):
            raise ValueError("baseline 은 같은 앱·같은 환경의 이전 배포여야 한다")
        return self


def load_spec(path: str | Path) -> DeploySpec:
    with open(path, encoding="utf-8") as f:
        return DeploySpec.model_validate(yaml.safe_load(f))
