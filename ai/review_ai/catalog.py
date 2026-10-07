"""catalog/rules.yaml · catalog/targets.yaml 로더. 읽기 전용이라 한 번만 읽는다."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

CATALOG_DIR = Path(__file__).resolve().parent.parent / "catalog"


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    severity: Literal["high", "medium", "low"]
    autofix: Literal["allowed", "forbidden", "when_no_data"]
    irreversible: bool
    priority: str
    providers: tuple[str, ...]
    title: str
    check: str
    fix: str

    def applies_to(self, env: str) -> bool:
        return "any" in self.providers or env in self.providers


@dataclass(frozen=True)
class TargetCaps:
    env: str
    verified: bool
    arch: frozenset[str]
    database: dict[str, dict[str, tuple[str, ...]]]  # placement → engine → 지원 버전 (빈 튜플 = 무관)
    volume_access_modes: frozenset[str]
    secret_sources: frozenset[str]
    ingress_class: str
    ingress_annotations: dict[str, str]

    def supports_db(self, placement: str, engine: str, version: str | None) -> bool:
        versions = self.database.get(placement, {}).get(engine)
        if versions is None:
            return False
        return not versions or version is None or version in versions


def _read(name: str) -> dict[str, Any]:
    return yaml.safe_load((CATALOG_DIR / name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Rule]:
    rules = {}
    for raw in _read("rules.yaml")["rules"]:
        rule = Rule(
            id=raw["id"],
            category=raw["category"],
            severity=raw["severity"],
            autofix=raw["autofix"],
            irreversible=bool(raw.get("irreversible", False)),
            priority=raw["priority"],
            providers=tuple(raw["providers"]),
            title=raw["title"],
            check=raw["check"],
            fix=raw["fix"],
        )
        rules[rule.id] = rule
    return rules


@lru_cache(maxsize=1)
def load_targets() -> dict[str, TargetCaps]:
    targets = {}
    for env, raw in _read("targets.yaml")["targets"].items():
        targets[env] = TargetCaps(
            env=env,
            verified=bool(raw["verified"]),
            arch=frozenset(raw["arch"]),
            database={
                placement: {engine: tuple(str(v) for v in versions) for engine, versions in engines.items()}
                for placement, engines in raw["database"].items()
            },
            volume_access_modes=frozenset(raw["volume_access_modes"]),
            secret_sources=frozenset(raw["secret_sources"]),
            ingress_class=raw["ingress"]["class"],
            ingress_annotations=dict(raw["ingress"].get("annotations") or {}),
        )
    return targets
