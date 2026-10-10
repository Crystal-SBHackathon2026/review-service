"""환경 선택 파일을 읽어 검증한 뒤, 멱등한 부모·자식 검토를 준비한다."""
from __future__ import annotations

from datetime import UTC, datetime

import yaml

from review_ai.messages import TOPIC, build_review_requested
from review_ai.spec.deploy_spec import AppSpec
from review_ai.spec.deployment_request import DeploymentRequest
from review_common.deployment_requests import request_id
from review_common.ids import new_review_id

MANIFEST_PATH = "deployment-request.yaml"
DEPLOYMENT_BRANCH = "main"  # sample-app image publishing runs only on this branch.
MAX_SPEC_BYTES = 256 * 1024


async def load_request(files, repository, sha, path, registry):
    raw = await files.get_file(repository, path, sha)
    if len(raw.encode()) > MAX_SPEC_BYTES:
        raise ValueError("deployment request is too large")
    manifest = DeploymentRequest.model_validate(yaml.safe_load(raw))
    specs = {}
    for target in manifest.targets:
        if (target.env, target.region, target.cluster) not in registry:
            raise ValueError(f"unregistered destination: {target.env}/{target.region}/{target.cluster}")
        if target.path == path:
            raise ValueError("manifest cannot also be an environment spec")
        text = await files.get_file(repository, target.path, sha)
        if len(text.encode()) > MAX_SPEC_BYTES:
            raise ValueError("environment spec is too large")
        spec = AppSpec.model_validate(yaml.safe_load(text))
        if spec.metadata.repository != repository:
            raise ValueError("spec repository does not match the request")
        if (spec.target.env, spec.target.region) != (target.env, target.region):
            raise ValueError("spec target does not match the selected environment")
        specs[target.env] = spec
    if len({s.metadata.name for s in specs.values()}) != 1:
        raise ValueError("one request must deploy the same app")
    if len({s.image.repository for s in specs.values()}) != 1:
        raise ValueError("one request must use the same image registry")
    return manifest, specs


async def dispatch_request(repo, files, publisher, repository, number, sha, path, requested_by, registry, *,
                           fix_count=0):
    manifest, specs = await load_request(files, repository, sha, path, registry)
    rid = request_id(repository, number, sha, path)
    await repo.insert_request(dict(request_id=rid, repository=repository, pr_number=number, head_sha=sha,
                                  manifest_path=path, targets=[t.model_dump() for t in manifest.targets],
                                  requested_by=requested_by, fix_count=fix_count))
    stored = await repo.get_request(rid)
    if stored["state"] == "superseded":
        raise ValueError("this request has been superseded")
    for target in manifest.targets:
        ref = dict(repository=repository, commit=sha, path=target.path)
        child = await repo.insert_review(review_id=new_review_id(), app=specs[target.env].metadata.name,
                                         target_env=target.env, repo_id=repository, spec_ref=ref, pr_head_sha=sha,
                                         requested_by=requested_by, pr_number=number, deployment_request_id=rid)
        row = await repo.get_review(child)
        if row["status"] != "received":
            continue
        msg = build_review_requested(specs[target.env].model_dump(mode="json", exclude_none=True), review_id=child,
                                     spec_ref=ref, requested_by=requested_by, requested_at=datetime.now(UTC),
                                     deployment_request_id=rid, autofix_commit=stored["fix_count"] > 0)
        # A failed publish leaves a received child. The normal review recovery sweep republishes it.
        await publisher.send(TOPIC, repository, msg.encode())
    return rid


async def supersede_request(repo, rid, error="new PR head"):
    await repo.update_request(rid, state="superseded", error=error)
    for child in await repo.request_children(rid):
        if child["status"] not in {"committed", "superseded"}:
            await repo.update_review(child["review_id"], status="superseded", error=error)
