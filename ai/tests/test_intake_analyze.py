"""레포 분석 — 근거가 분명한 값만 채우고, 애매하면 비워 확인 항목으로 남긴다."""

from __future__ import annotations

import json

import pytest
import yaml

from review_ai.intake import prepare_intake
from review_ai.intake.analyze import MAX_SOURCE_FILES, RepoAnalysis, analyze_repository, files_to_read
from review_ai.preparation import GenerationContext
from review_ai.spec.deploy_spec import BASE_RESOURCES, AppSpec, Database, Health, Image, Requirements, Runtime, Storage

REPO = "Crystal-SBHackathon2026/sample-app"
BASE = GenerationContext(repository=REPO, target={"env": "aws", "region": "ap-northeast-2"})

# sample-app 14411fd 의 실제 파일을 줄인 것
DOCKERFILE = """FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
USER node
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
CMD ["node", "src/server.js"]
"""
WORKFLOW = """env:
  IMAGE: ghcr.io/crystal-sbhackathon2026/sample-app
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: docker build -t "$IMAGE:${{ github.sha }}" .
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
"""
APP_JS = """const express = require("express");
const info = { version: process.env.APP_VERSION || "dev", environment: process.env.DEPLOY_ENV || "local" };
const failRate = Number(process.env.FAIL_RATE || 0);
const healthFail = process.env.HEALTH_FAIL === "true";
"""
SERVER_JS = 'const port = Number(process.env.PORT || 8080);\n'


def sample_files(**changes: str | None) -> dict[str, str]:
    files = {
        "Dockerfile": DOCKERFILE,
        "package.json": json.dumps({"name": "sample-app", "dependencies": {"express": "^4.21.2"},
                                    "devDependencies": {"pg": "^8"}}),
        ".github/workflows/ci.yml": WORKFLOW,
        "src/app.js": APP_JS,
        "src/server.js": SERVER_JS,
    }
    files.update(changes)
    return {path: text for path, text in files.items() if text is not None}


def analyze(files: dict[str, str], extra_tree: tuple[str, ...] = ()) -> RepoAnalysis:
    tree = [*files, "package-lock.json", "README.md", "test/app.test.js", *extra_tree]
    return analyze_repository(BASE, tree, files)


def reason(analysis: RepoAnalysis, path: str) -> str:
    return next(f.reason for f in analysis.findings if f.path == path)


def test_sample_app_is_fully_resolved_and_committable() -> None:
    analysis = analyze(sample_files())

    assert analysis.unresolved == {}
    ctx = analysis.context
    assert ctx.image == Image(repository="ghcr.io/crystal-sbhackathon2026/sample-app", platforms=("amd64",))
    assert ctx.runtime == Runtime(port=8080, health=Health(readiness="/healthz", liveness="/healthz"), resources=BASE_RESOURCES)
    assert (ctx.database, ctx.storage, ctx.secrets, ctx.requirements) == (
        Database(), Storage(), (), Requirements(persistence=False))  # devDependencies 의 pg 는 실행에 없다
    assert "APP_VERSION, DEPLOY_ENV, FAIL_RATE, HEALTH_FAIL, NODE_ENV, PORT" in reason(analysis, "/secrets")

    outcome = prepare_intake("missing", context=ctx, findings=analysis.findings)
    assert (outcome.action, outcome.reason) == ("generated", "GENERATED") and "레포 분석" in outcome.message
    spec = AppSpec.model_validate(yaml.safe_load(outcome.content))
    assert (spec.metadata.name, spec.runtime.port, spec.runtime.health.readiness) == ("sample-app", 8080, "/healthz")
    assert {"path": "/runtime", "source": "Dockerfile", "reason": "EXPOSE 8080, HEALTHCHECK /healthz"} in outcome.details


def test_files_to_read_skips_tests_and_vendored_code() -> None:
    tree = ["Dockerfile", "go.mod", ".github/workflows/ci.yml", ".github/dependabot.yml", "src/a.ts", "src/a.d.ts",
            "src/a.test.ts", "test/b.js", "node_modules/x/index.js", "pkg/c_test.go", "pkg/c.go", "test_d.py", "e.py"]

    assert files_to_read(tree) == ("Dockerfile", "go.mod", ".github/workflows/ci.yml", "e.py", "pkg/c.go", "src/a.ts")


def test_unresolved_reasons_reach_unverified_details() -> None:
    analysis = analyze(sample_files(**{"src/db.js": "const { Pool } = require('pg');\n",
                                       "package.json": json.dumps({"dependencies": {"pg": "^8"}})}))
    outcome = prepare_intake("missing", context=analysis.context, findings=analysis.findings)

    assert (outcome.action, outcome.reason) == ("rejected", "UNVERIFIED")
    database = next(d for d in outcome.details if d["path"] == "/database")
    assert database["source"] == "package.json" and "pg → postgres" in database["message"]
    assert {d["code"] for d in outcome.details} == {"DATABASE_UNVERIFIED", "PERSISTENCE_UNVERIFIED"}


# --- runtime --------------------------------------------------------------------------------------

@pytest.mark.parametrize(("dockerfile", "expected"), [
    ("FROM node AS build\nEXPOSE 3000\nFROM node\nEXPOSE 8080/tcp 9229/udp\n", Runtime(port=8080, resources=BASE_RESOURCES)),
    ("FROM x\nEXPOSE 8080\nHEALTHCHECK CMD curl -f http://localhost/ || exit 1\n", Runtime(port=8080, resources=BASE_RESOURCES)),
    ("FROM x\nEXPOSE 80\nHEALTHCHECK CMD curl -f http://localhost/ || exit 1\n",
     Runtime(port=80, health=Health(readiness="/", liveness="/"), resources=BASE_RESOURCES)),
    ("FROM x\n# EXPOSE 1\nEXPOSE \\\n  5000\nHEALTHCHECK --interval=5s \\\n  CMD wget -q http://[::1]:5000/ready?x=1\n",
     Runtime(port=5000, health=Health(readiness="/ready", liveness="/ready"), resources=BASE_RESOURCES)),
    ("FROM x\nEXPOSE 8080\nHEALTHCHECK NONE\n", Runtime(port=8080, resources=BASE_RESOURCES)),
])
def test_runtime_from_final_stage(dockerfile: str, expected: Runtime) -> None:
    assert analyze(sample_files(Dockerfile=dockerfile)).context.runtime == expected


@pytest.mark.parametrize(("dockerfile", "why"), [
    (None, "Dockerfile 이 없어"),
    ("FROM x\nCMD run\n", "(없음)"),
    ("FROM x\nEXPOSE 8080 9090\n", "(8080, 9090)"),
    ("FROM x\nEXPOSE $PORT\n", "($PORT)"),
])
def test_runtime_left_unverified(dockerfile: str | None, why: str) -> None:
    analysis = analyze(sample_files(Dockerfile=dockerfile))

    assert analysis.context.runtime is None and why in analysis.unresolved["/runtime"].reason


# --- image ----------------------------------------------------------------------------------------

def test_image_from_templated_repository_and_declared_platforms() -> None:
    workflow = ("jobs:\n  b:\n    runs-on: self-hosted\n    steps:\n      - uses: docker/build-push-action@v6\n"
                "        with:\n          platforms: linux/amd64,linux/arm64\n"
                "          tags: ghcr.io/${{ github.repository }}:${{ github.sha }}\n")
    image = analyze(sample_files(**{".github/workflows/ci.yml": workflow})).context.image

    assert image == Image(repository="ghcr.io/crystal-sbhackathon2026/sample-app", platforms=("amd64", "arm64"))


@pytest.mark.parametrize(("workflow", "why"), [
    (None, "CI 워크플로가 없어"),
    ("env:\n  IMAGE: docker.io/me/app\n", "(없음)"),
    ("tags: ghcr.io/a/x\n---\ntags: ghcr.io/a/y\n", "ghcr.io/a/x, ghcr.io/a/y"),
    ("tags: ghcr.io/${{ env.OWNER }}/app\n", "${{"),
    ("tags: ghcr.io/a/x,ghcr.io/a/x-dev\n", "ghcr.io/a/x, ghcr.io/a/x-dev"),
    ("tags: ghcr.io/a/x\njobs:\n  b:\n    runs-on: ubuntu-24.04-arm\n", "빌드 플랫폼"),
])
def test_image_left_unverified(workflow: str | None, why: str) -> None:
    analysis = analyze(sample_files(**{".github/workflows/ci.yml": workflow}))

    assert analysis.context.image is None and why in analysis.unresolved["/image"].reason


# --- database·requirements -----------------------------------------------------------------------

@pytest.mark.parametrize(("manifest", "text", "why"), [
    ("package.json", json.dumps({"dependencies": {"mysql2": "3"}}), "mysql2 → mysql"),
    ("package.json", json.dumps({"dependencies": {"@prisma/client": "5"}}), "ORM·다른 저장소 의존성 @prisma/client"),
    ("package.json", "{not json", "package.json 을 읽지 못했다"),
    ("requirements.txt", "Flask==3.0\npsycopg2_binary>=2.9 ; python_version>'3'\n", "psycopg2-binary → postgres"),
    ("requirements.txt", "-r base.txt\n", "-r·-c"),
    ("pyproject.toml", '[project]\ndependencies = ["SQLAlchemy>=2"]\n', "sqlalchemy"),
    ("pyproject.toml", '[tool.poetry.dependencies]\npython = "^3.12"\naiosqlite = "*"\n', "aiosqlite → sqlite"),
    ("pyproject.toml", "[project\n", "pyproject.toml 을 읽지 못했다"),
    ("go.mod", "module x\n\nrequire (\n\tgithub.com/jackc/pgx/v5 v5.5.0 // indirect\n)\n", "pgx/v5 → postgres"),
    ("go.mod", "module x\nrequire github.com/mattn/go-sqlite3 v1.14.0\n", "go-sqlite3 → sqlite"),
])
def test_database_drivers_and_orms_stay_unverified(manifest: str, text: str, why: str) -> None:
    analysis = analyze(sample_files(**{"package.json": None, manifest: text}))

    assert analysis.context.database is None and why in analysis.unresolved["/database"].reason
    assert analysis.context.requirements is None  # DB 를 모르면 보존 요구도 모른다


def test_database_needs_a_manifest() -> None:
    analysis = analyze(sample_files(**{"package.json": None}))

    assert "의존성 파일" in analysis.unresolved["/database"].reason
    assert "/storage" in analysis.unresolved


@pytest.mark.parametrize(("path", "code"), [
    ("src/store.js", "const { DatabaseSync } = require('node:sqlite');\n"),
    ("app/db.py", "import sqlite3\n"),
    ("main.go", 'import "database/sql"\n'),
])
def test_stdlib_database_use_is_detected(path: str, code: str) -> None:
    analysis = analyze(sample_files(**{path: code}))

    assert analysis.unresolved["/database"].source == path


def test_too_many_sources_keeps_absence_claims_unverified() -> None:
    files = sample_files()
    extra = tuple(f"src/m{i:02}.js" for i in range(MAX_SOURCE_FILES))
    tree = [*files, *extra]
    read = {p: files.get(p, "module.exports = {};\n") for p in files_to_read(tree)}
    analysis = analyze_repository(BASE, tree, read)

    assert set(analysis.unresolved) == {"/database", "/storage", "/secrets", "/requirements"}
    assert analysis.context.runtime is not None and analysis.context.image is not None


@pytest.mark.parametrize(("extra", "why"), [
    ({"src/main/java/App.java": 'String pw = System.getenv("DB_PASSWORD");'}, "src/main/java/App.java"),
    ({"docker-entrypoint.sh": "exec node src/server.js\n"}, "docker-entrypoint.sh"),
    ({"server/package.json": json.dumps({"dependencies": {"pg": "8"}})}, "server/package.json"),
    ({"Pipfile": "[packages]\npsycopg2 = '*'\n"}, "Pipfile"),
])
def test_unscanned_languages_or_manifests_block_absence_claims(extra: dict[str, str], why: str) -> None:
    """루트 package.json 만 보고 Java·Pipfile 백엔드에 'DB·시크릿 없음'을 커밋하면 안 된다."""
    analysis = analyze(sample_files(**extra))

    assert set(analysis.unresolved) == {"/database", "/storage", "/secrets", "/requirements"}
    assert why in analysis.unresolved["/secrets"].reason


def test_unscanned_list_is_shortened() -> None:
    extra = {f"lib/m{i}.rb": "" for i in range(5)}
    assert "외 2개" in analyze(sample_files(**extra)).unresolved["/database"].reason


def test_frontend_bundles_are_not_scanned() -> None:
    analysis = analyze(sample_files(**{"public/vendor.js": "x(process.env)", "src/lib.min.js": "x(process.env)"}))

    assert analysis.unresolved == {}


# --- secrets --------------------------------------------------------------------------------------

@pytest.mark.parametrize(("code", "why"), [
    ("const k = process.env.STRIPE_API_KEY;\n", "STRIPE_API_KEY"),
    ("import os\npw = os.environ['DB_PASSWORD']\n", "DB_PASSWORD"),
    ('tok := os.Getenv("GITHUB_TOKEN")\n', "GITHUB_TOKEN"),
    ("const env = cleanEnv(process.env, {});\n", "이름 없이"),
    ("const v = process.env[name];\n", "이름 없이"),
    ("import os\nconfig = dict(os.environ)\n", "이름 없이"),
    ("class S(BaseSettings):\n    pass\n", "이름 없이"),
])
def test_secret_env_reads_stay_unverified(code: str, why: str) -> None:
    analysis = analyze(sample_files(**{"src/config.js": code}))

    assert analysis.context.secrets is None and why in analysis.unresolved["/secrets"].reason


@pytest.mark.parametrize(("manifest", "text", "lib"), [
    ("requirements.txt", "flask\npython-decouple==3.8\n", "python-decouple"),
    ("package.json", json.dumps({"dependencies": {"express": "4", "convict": "6"}}), "convict"),
    ("go.mod", "module x\nrequire github.com/kelseyhightower/envconfig v1.4.0\n", "envconfig"),
])
def test_config_libraries_hide_env_names(manifest: str, text: str, lib: str) -> None:
    analysis = analyze(sample_files(**{"package.json": None, manifest: text}))

    assert analysis.context.secrets is None and lib in analysis.unresolved["/secrets"].reason


def test_secret_name_in_dockerfile_env_counts() -> None:
    analysis = analyze(sample_files(Dockerfile=DOCKERFILE.replace("ENV NODE_ENV=production",
                                                                  "ARG NPM_TOKEN\nENV NODE_ENV=production")))

    assert "NPM_TOKEN" in analysis.unresolved["/secrets"].reason


def test_named_env_reads_with_get_are_not_dynamic() -> None:
    code = "import os\nport = os.environ.get('PORT')\nmode = os.getenv('MODE')\nx = os.environ['REGION']\n"
    analysis = analyze(sample_files(**{"app/main.py": code}))

    assert analysis.context.secrets == () and "MODE" in reason(analysis, "/secrets")


# --- storage --------------------------------------------------------------------------------------

@pytest.mark.parametrize(("change", "why"), [
    ({"Dockerfile": DOCKERFILE + "VOLUME /data\n"}, "VOLUME /data"),
    ({"package.json": json.dumps({"dependencies": {"multer": "1"}})}, "multer"),
    ({"src/save.js": "fs.writeFileSync('/tmp/x', data);\n"}, "파일을 쓰는 코드"),
    ({"app/save.py": "with open(path, 'wb') as f:\n    f.write(b)\n"}, "파일을 쓰는 코드"),
    ({"app/save.py": "f = open(path, mode='a')\n"}, "파일을 쓰는 코드"),
    ({"app/save.py": "with Path(p).open('w') as f:\n    pass\n"}, "파일을 쓰는 코드"),
    ({"main.go": "f, _ := os.Create(name)\n"}, "파일을 쓰는 코드"),
])
def test_storage_signals_stay_unverified(change: dict[str, str], why: str) -> None:
    analysis = analyze(sample_files(**change))

    assert analysis.context.storage is None and why in analysis.unresolved["/storage"].reason


def test_context_values_already_supplied_are_kept() -> None:
    runtime = Runtime(port=9000)
    base = BASE.model_copy(update={"runtime": runtime})

    analysis = analyze_repository(base, list(sample_files()), sample_files())

    assert analysis.context.runtime == runtime
