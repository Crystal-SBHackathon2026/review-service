"""다중 환경 GitOps: 실제 kustomize 검사 후 하나의 tree/ref 갱신으로 릴리스를 기록한다."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile

import yaml

from review_ai.overlay import render_overlay
from review_common.github import FILE_MODE, ProtectedFileRemoval, RefConflict, git_blob_sha, is_protected
from review_worker.commit_overlay import PROTECTED_OVERLAY_FILES

ENVS = ("aws", "gcp", "local")


class RequestGitOps:
    def __init__(self, git, *, kustomize_command=None):
        self.git = git
        self.command = kustomize_command or [os.environ.get("KUBECTL_BIN", "kubectl"), "kustomize"]

    async def plan(self, specs, targets, image_tag, retry_id=None):
        gh, repo = self.git._gh, self.git.gitops_repo
        head = await gh.branch_sha(repo, self.git.branch)
        blobs = await gh.tree_blobs(repo, await gh.commit_tree_sha(repo, head))
        app = next(iter(specs.values())).metadata.name
        prefix = f"apps/{app}/"
        paths = [p for p in blobs if p.startswith(prefix) and p.endswith((".yaml", ".yml"))]
        paths += [f"argocd/{app}-{t['env']}.yaml" for t in targets]
        if len(paths) > 100:
            raise ValueError("GitOps snapshot exceeds 100 files")
        snapshot = {}
        for path in paths:
            if path not in blobs:
                raise ValueError(f"missing registered application: {path}")
            text = await gh.get_file(repo, path, head, max_bytes=256 * 1024)
            snapshot[path] = text
        if sum(len(v.encode()) for v in snapshot.values()) > 2 * 1024 * 1024:
            raise ValueError("GitOps snapshot is too large")
        base = yaml.safe_load(snapshot[prefix + "base/kustomization.yaml"])
        image = next(iter(specs.values())).image.repository
        original = next((v for v in base.get("images", []) if v["name"] == image), None)
        if original is None or not (original.get("newTag") or original.get("digest")):
            raise ValueError("base must provide an explicit image version for the selected app")
        files = {}
        for target in targets:
            env = target["env"]
            spec = specs[env]
            argo = yaml.safe_load(snapshot[f"argocd/{app}-{env}.yaml"])["spec"]
            dest = argo["destination"]
            destination = dest.get("name") or ("in-cluster" if dest.get("server") ==
                                                "https://kubernetes.default.svc" else None)
            sources = argo.get("sources", [argo.get("source", {})])
            directory = prefix + f"overlays/{env}"
            if destination != target["cluster"] or dest.get("namespace") != (spec.target.namespace or app):
                raise ValueError(f"Argo destination mismatch: {env}")
            if not any(s.get("path") == directory and s.get("targetRevision") == self.git.branch for s in sources):
                raise ValueError(f"Argo source mismatch: {env}")
            kpath = directory + "/kustomization.yaml"
            if retry_id:
                # A retry restarts the configuration currently deployed, including any
                # operator fixes. Re-rendering the old AppSpec would revert those fixes.
                k = yaml.safe_load(snapshot[kpath])
                current = next((v for v in k.get("images", []) if v["name"] == image), original)
                if current.get("newTag") != image_tag or current.get("digest"):
                    raise ValueError("cannot retry an environment running a newer/different release")
                patch = {"target": {"kind": "Rollout", "name": app}, "patch": yaml.safe_dump({
                    "apiVersion": "argoproj.io/v1alpha1", "kind": "Rollout", "metadata": {"name": app},
                    "spec": {"template": {"metadata": {"annotations": {"oneaction.crystal/retry": retry_id}}}}})}
                patches = k.setdefault("patches", [])
                if patch not in patches:
                    patches.append(patch)
                files[kpath] = yaml.safe_dump(k, sort_keys=False)
                continue
            rendered = render_overlay(spec)
            if rendered.blocking:
                raise ValueError(" / ".join(f"[{w.code}] {w}" for w in rendered.blocking))
            wanted = {f"{directory}/{name}": text for name, text in rendered.files.items()}
            removed = [p for p in blobs if p.startswith(directory + "/") and p not in wanted]
            protected = [p for p in removed if is_protected(p[len(directory) + 1:], PROTECTED_OVERLAY_FILES)]
            if protected:
                raise ProtectedFileRemoval(protected)
            files.update(dict.fromkeys(removed))
            files.update(wanted)
            k = yaml.safe_load(files[kpath])
            k["images"] = [{"name": image, "newTag": image_tag}]
            files[kpath] = yaml.safe_dump(k, sort_keys=False)
        # Freeze unselected environments at their current version. This is a one-time metadata pin;
        # their resolved manifests stay the same even if legacy CI later moves base's tag.
        for env in (() if retry_id else ENVS):
            if env in specs:
                continue
            path = prefix + f"overlays/{env}/kustomization.yaml"
            if path not in snapshot:
                continue
            k = yaml.safe_load(snapshot[path])
            if not any(v["name"] == image for v in k.get("images", [])):
                k.setdefault("images", []).append(original.copy())
                files[path] = yaml.safe_dump(k, sort_keys=False)
        merged = {**snapshot, **files}
        await self.validate({p: v for p, v in merged.items() if v is not None}, app, specs)
        return head, blobs, files

    async def validate(self, snapshot, app, specs):
        with tempfile.TemporaryDirectory(prefix="oneaction-render-") as folder:
            root = Path(folder)
            for path, text in snapshot.items():
                dest = root / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Never fetch remote bases or execute generator plugins during a review.
                if path.endswith("kustomization.yaml"):
                    k = yaml.safe_load(text)
                    for key in ("resources", "bases", "components"):
                        for value in k.get(key, []):
                            if "://" in str(value) or str(value).startswith("git@"):
                                raise ValueError("remote Kustomize resources are not allowed")
                            if not (dest.parent / str(value)).resolve().is_relative_to(root.resolve()):
                                raise ValueError("Kustomize resource escapes the isolated snapshot")
                dest.write_text(text)
            for env in specs:
                proc = await asyncio.create_subprocess_exec(*self.command, str(root / f"apps/{app}/overlays/{env}"),
                                                            stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                except BaseException:
                    proc.kill()
                    await proc.wait()
                    raise
                if proc.returncode:
                    raise ValueError(f"Kustomize build failed: {env}")
                docs = list(yaml.safe_load_all(stdout))
                if not any(d and d.get("kind") == "Rollout" for d in docs):
                    raise ValueError(f"Kustomize produced no Rollout: {env}")

    def selected_snapshot(self, blobs, specs):
        app = next(iter(specs.values())).metadata.name
        prefixes = [f"apps/{app}/base/"] + [f"apps/{app}/overlays/{env}/" for env in specs]
        return {p: sha for p, sha in blobs.items() if any(p.startswith(prefix) for prefix in prefixes)}

    async def preflight(self, specs, targets, tag):
        _, blobs, _ = await self.plan(specs, targets, tag)
        return self.selected_snapshot(blobs, specs)

    async def commit(self, specs, targets, tag, request_id, expected_snapshot=None, retry_id=None):
        for _ in range(self.git.max_attempts):
            head, blobs, files = await self.plan(specs, targets, tag, retry_id)
            entries = []
            for path, content in sorted(files.items()):
                if content is None:
                    entries.append(dict(path=path, mode=FILE_MODE, type="blob", sha=None))
                elif blobs.get(path) != git_blob_sha(content):
                    entries.append(dict(path=path, mode=FILE_MODE, type="blob", content=content))
            if not entries:
                return head
            if expected_snapshot is not None and self.selected_snapshot(blobs, specs) != expected_snapshot:
                raise ValueError("selected GitOps configuration changed after review; re-review required")
            gh, repo = self.git._gh, self.git.gitops_repo
            tree = await gh.create_tree(repo, await gh.commit_tree_sha(repo, head), entries)
            sha = await gh.create_commit(repo, message=f"deploy: {request_id} image {tag}", tree=tree, parents=[head])
            try:
                await gh.update_branch(repo, self.git.branch, sha)
                return sha
            except RefConflict:
                continue  # Re-read, re-render, and re-check protected resources against the new head.
        raise RefConflict("GitOps ref kept changing", 422)
