"""스모크 — 테스트가 없는 앱이면 레포 분석이 확인 경로를 채우고, 렌더러가 PostSync Job 으로 만든다."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from review_ai.intake import prepare_intake
from review_ai.intake.analyze import analyze_repository
from review_ai.overlay import render_overlay
from review_ai.overlay.plan import SMOKE_IMAGE
from review_ai.spec.deploy_spec import AppSpec, DeploySpec, Smoke
from tests.conftest import load_sample_dict
from tests.test_deploy_plan import _build_all, _one
from tests.test_intake_analyze import BASE, sample_files
from tests.test_overlay import needs_kubectl, write_rendered

ROUTES_JS = """const app = require("express")();
app.get("/healthz", (req, res) => res.send("ok"));
app.get("/api/info", info);
app.get("/api/todos", requireLogin, list);
app.get("/todos/:id", one);
app.post("/todos", create);
"""


def no_tests(files: dict[str, str], *extra: str) -> list[str]:
    return [*files, "package-lock.json", "README.md", *extra]


def smoke_finding(analysis) -> str:  # type: ignore[no-untyped-def]
    return next(f.reason for f in analysis.findings if f.path == "/smoke")


def test_repo_with_tests_gets_no_smoke() -> None:
    files = sample_files(**{"src/app.js": ROUTES_JS})
    analysis = analyze_repository(BASE, [*no_tests(files), "test/app.test.js"], files)
    assert analysis.context.smoke is None
    assert "테스트가 있어" in smoke_finding(analysis)
    assert analysis.unresolved == {}


@pytest.mark.parametrize("test_file", ["src/app.spec.ts", "tests/test_api.py", "pkg/x_test.go", "__tests__/a.js"])
def test_any_test_layout_counts_as_tests(test_file: str) -> None:
    files = sample_files(**{"src/app.js": ROUTES_JS})
    assert analyze_repository(BASE, no_tests(files, test_file), files).context.smoke is None


def test_repo_without_tests_checks_readiness_and_fixed_get_routes() -> None:
    files = sample_files(**{"src/app.js": ROUTES_JS})
    analysis = analyze_repository(BASE, no_tests(files), files)
    # readiness 가 먼저, 업무 경로(/api/todos — 로그인 필요)·파라미터 경로(:id)·POST 는 빠진다, 중복은 한 번
    assert analysis.context.smoke == Smoke(paths=("/healthz", "/api/info"))
    assert "/healthz, /api/info" in smoke_finding(analysis)


@pytest.mark.parametrize(("path", "source", "route"), [
    ("app/main.py", '@app.get("/status")\n@app.get("/items")\ndef status(): ...\n', "/status"),
    ("app/views.py", "@bp.route('/ping')\ndef ping(): ...\n", "/ping"),
    ("main.go", 'http.HandleFunc("GET /livez", live)\nhttp.HandleFunc("/orders", o)\n', "/livez"),
    ("server.go", 'r.GET("/v1/status", h)\n', "/v1/status"),
])
def test_routes_in_other_stacks(path: str, source: str, route: str) -> None:
    files = {"Dockerfile": "FROM x\nEXPOSE 8080\n", path: source}
    assert analyze_repository(BASE, list(files), files).context.smoke.paths == (route,)  # type: ignore[union-attr]


def test_no_tests_and_no_routes_leaves_smoke_empty_without_blocking() -> None:
    files = sample_files(**{"Dockerfile": "FROM node\nEXPOSE 8080\n"})
    analysis = analyze_repository(BASE, no_tests(files), files)
    assert analysis.context.smoke is None
    outcome = prepare_intake("missing", context=analysis.context, findings=analysis.findings)
    assert outcome.action == "generated"  # 확인 경로는 선택 항목이라 생성을 막지 않는다


def test_generated_spec_carries_smoke_paths() -> None:
    files = sample_files(**{"src/app.js": ROUTES_JS})
    analysis = analyze_repository(BASE, no_tests(files), files)
    outcome = prepare_intake("missing", context=analysis.context, findings=analysis.findings)
    spec = AppSpec.model_validate(yaml.safe_load(outcome.content))
    assert spec.smoke == Smoke(paths=("/healthz", "/api/info"))
    assert any(d["path"] == "/smoke" for d in outcome.details)


@pytest.mark.parametrize("paths", [[], ["healthz"], ["/a b"], [f"/p{i}" for i in range(11)]])
def test_smoke_paths_are_validated(paths: list[str]) -> None:
    raw = {**load_sample_dict("01-pass-sample-app-aws.yaml"), "smoke": {"paths": paths}}
    with pytest.raises(ValidationError):
        DeploySpec.model_validate(raw)


def test_no_smoke_renders_no_job() -> None:
    rendered = render_overlay(DeploySpec.model_validate(load_sample_dict("01-pass-sample-app-aws.yaml")))
    assert "job-smoke.yaml" not in rendered.files


@needs_kubectl
def test_smoke_job_gets_each_path_through_the_service_after_sync(tmp_path: Path) -> None:
    raw = {**load_sample_dict("01-pass-sample-app-aws.yaml"), "smoke": {"paths": ["/healthz", "/api/info"]}}
    docs = _build_all(write_rendered(tmp_path, DeploySpec.model_validate(raw)))
    job = _one(docs, "Job", "sample-app-smoke")
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PostSync"
    assert job["metadata"]["namespace"] == "sample-app"
    [container] = job["spec"]["template"]["spec"]["containers"]
    assert container["image"] == SMOKE_IMAGE  # 앱 이미지 치환(replacements) 대상이 아니다
    assert container["command"][:2] == ["sh", "-c"]
    # 경로는 스크립트가 아니라 인자로 — 셸 문자열에 끼워 넣지 않는다
    assert container["command"][4:] == ["http://sample-app:80/healthz", "http://sample-app:80/api/info"]
    assert container["securityContext"]["runAsNonRoot"] is True
