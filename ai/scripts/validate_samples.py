"""샘플·규칙 목록·기대 결과가 서로 맞는지 확인하고 JSON Schema 를 내보낸다.

    .venv/bin/python scripts/validate_samples.py

확인하는 것
- samples/*.yaml 이 전부 DeploySpec 형식을 통과한다
- cases.yaml 과 샘플 파일 목록이 1:1 이다
- cases.yaml 의 rule_id 가 catalog/rules.yaml 에 있다
- rules.yaml 의 ID 가 중복 없고 접두가 카테고리와 맞다
- 형식 오류 예시는 거절된다 (Review API 422 경계)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review_ai.spec.deploy_spec import DeploySpec, load_spec  # noqa: E402
from review_ai.state import REASON_CODES  # noqa: E402

REASONS = set(REASON_CODES)

PREFIX = {"database": "DB", "secret": "SEC", "network": "NET", "storage": "STO", "runtime": "RUN"}
VERDICTS = {"pass", "fix", "needs_human"}


def check_rules() -> set[str]:
    rules = yaml.safe_load((ROOT / "catalog/rules.yaml").read_text(encoding="utf-8"))["rules"]
    ids = [r["id"] for r in rules]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"규칙 ID 중복: {dupes}"
    for r in rules:
        assert r["id"].split("-")[0] == PREFIX[r["category"]], f"{r['id']}: 접두와 카테고리 불일치"
        assert r["severity"] in {"high", "medium", "low"}, r["id"]
        assert r["autofix"] in {"allowed", "forbidden", "when_no_data"}, r["id"]
    return set(ids)


def check_samples(rule_ids: set[str]) -> int:
    sample_dir = ROOT / "samples"
    cases = yaml.safe_load((sample_dir / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    files = sorted(p.name for p in sample_dir.glob("[0-9]*.yaml"))
    listed = sorted(c["file"] for c in cases)
    assert files == listed, f"cases.yaml 과 샘플 목록 불일치: {set(files) ^ set(listed)}"

    for case in cases:
        load_spec(sample_dir / case["file"])
        unknown = set(case["findings"]) - rule_ids
        assert not unknown, f"{case['file']}: 목록에 없는 규칙 {unknown}"
        assert case["verdict"] in VERDICTS, case["file"]
        assert set(case["reasons"]) <= REASONS, case["file"]
        if case["verdict"] == "fix":
            assert "after_patch" in case, f"{case['file']}: fix 는 after_patch 기대값 필요"
    return len(cases)


def check_rejects() -> int:
    base = yaml.safe_load((ROOT / "samples/01-pass-sample-app-aws.yaml").read_text(encoding="utf-8"))
    broken = {
        "engine 에 placement 없음": {**base, "database": {"engine": "postgres"}},
        "모르는 필드": {**base, "replica": 3},
        "tag·digest 둘 다 없음": {**base, "image": {"repository": "x", "platforms": ["amd64"]}},
        "시크릿 key 없음": {**base, "secrets": [{"name": "API_KEY", "source": "k8s-secret"}]},
        "소문자 env 이름": {**base, "runtime": {**base["runtime"], "env": {"deploy_env": "aws"}}},
    }
    for label, doc in broken.items():
        try:
            DeploySpec.model_validate(doc)
        except ValidationError:
            continue
        raise AssertionError(f"형식 오류가 통과됨: {label}")
    return len(broken)


def export_schema() -> Path:
    out = ROOT / "schema/deploy_spec.schema.json"
    out.write_text(json.dumps(DeploySpec.model_json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def main() -> None:
    rule_ids = check_rules()
    n_cases = check_samples(rule_ids)
    n_rejects = check_rejects()
    out = export_schema()
    print(f"규칙 {len(rule_ids)}개 · 샘플 {n_cases}개 통과 · 형식 오류 {n_rejects}개 거절 확인 · {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
