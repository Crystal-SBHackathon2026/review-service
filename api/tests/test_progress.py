"""진행 화면 API (GET /reviews/{id}/progress) 와 화면 경로 (/ui/reviews). 토큰 없이 읽는다."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from review_api.app import ApiDeps, create_app
from review_api.argocd import ArgoCdEvent, handle_deploy_event
from review_api.progress import ProgressSettings, parse_app_urls
from review_common.repository import InMemoryReviewRepository
from review_ai.secrets_pattern import MASK

REPO = "Crystal-SBHackathon2026/sample-app"
IMAGE = "ghcr.io/crystal-sbhackathon2026/sample-app"
MERGE = "3c7ad2e366ac735ad040419f49ce8df216d44e27"
GITOPS = "0f94208c1d2e3f4a5b6c7d8e9f00112233445566"
AWS_URL = "http://k8s-sampleapp-new.ap-northeast-2.elb.amazonaws.com"
LOCAL_URL = "http://sample-app.localtest.me:8080"


class NoSpecs:
    async def get_file(self, repository: str, path: str, ref: str) -> str:
        raise AssertionError("진행 화면은 명세를 읽지 않는다")


class NoPublisher:
    async def send(self, topic: str, key: str, value: bytes) -> None:
        raise AssertionError("진행 화면은 발행하지 않는다")


class Env:
    def __init__(self) -> None:
        self.repo = InMemoryReviewRepository()
        settings = ProgressSettings(app_urls={"sample-app": {"aws": AWS_URL, "local": LOCAL_URL}})
        self.client = TestClient(create_app(ApiDeps(repo=self.repo, specs=NoSpecs(), publisher=NoPublisher(),
                                                    progress=settings)))  # 토큰 헤더 없이

    async def review(self, rid: str, *, head: str, requested_by: str = "hyeyeon", **fields: Any) -> str:
        await self.repo.insert_review(review_id=rid, app="sample-app", target_env="aws", repo_id=REPO,
                                      spec_ref={"repository": REPO, "commit": head, "path": "deploy.yaml"},
                                      pr_head_sha=head, requested_by=requested_by, pr_number=12)
        if fields:
            await self.repo.update_review(rid, **fields)
        return rid

    def progress(self, rid: str) -> dict[str, Any]:
        resp = self.client.get(f"/reviews/{rid}/progress")
        assert resp.status_code == 200, resp.text
        return resp.json()


@pytest.fixture
def env() -> Env:
    return Env()


def states(body: dict[str, Any]) -> dict[str, str]:
    return {s["key"]: s["state"] for s in body["steps"]}


def step(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(s for s in body["steps"] if s["key"] == key)


def card(body: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in body["envs"] if c["env"] == name)


COMMITTED = {"status": "committed", "verdict": "pass", "merge_sha": MERGE, "gitops_commit_sha": GITOPS,
             "deploy_result": {"status": "committed", "commit_sha": GITOPS, "reason": None},
             "findings": [], "rounds": [], "decision": {"verdict": "pass", "reasons": [], "items": []}}


def aws_event(health: str = "Healthy", env_name: str = "aws") -> ArgoCdEvent:
    return ArgoCdEvent(app="sample-app", env=env_name, health=health, images=[f"{IMAGE}:{MERGE[:7]}"])


async def test_pass_auto_merged_and_deployed(env: Env) -> None:
    rid = await env.review("rv_pass", head="a" * 40, **COMMITTED)
    await handle_deploy_event(env.repo, aws_event())

    body = env.progress(rid)

    assert [s["key"] for s in body["steps"]] == ["review", "ci", "merge", "gitops", "deploy"]  # 사람 확인 없음
    assert set(states(body).values()) == {"done"}
    assert step(body, "gitops")["detail"] == f"overlay 커밋 {GITOPS[:7]}"
    assert step(body, "deploy")["at"] is not None
    assert step(body, "deploy")["detail"] == "aws Healthy · local 알림 대기"
    assert card(body, "aws") == {
        "env": "aws", "is_target": True, "app_url": AWS_URL,
        "deploy": {"kind": "healthy", "image_tag": MERGE[:7], "received_at": card(body, "aws")["deploy"]["received_at"]},
        "render": {"status": "committed", "reason": None, "gitops_commit_sha": GITOPS}}
    assert card(body, "local") == {"env": "local", "is_target": False, "deploy": None, "app_url": LOCAL_URL}
    assert card(body, "gcp") == {"env": "gcp", "planned": True}
    assert body["links"] == {"pr": f"https://github.com/{REPO}/pull/12",
                             "merge_commit": f"https://github.com/{REPO}/commit/{MERGE}",
                             "gitops_commit": f"https://github.com/Crystal-SBHackathon2026/gitops/commit/{GITOPS}"}
    assert body["final"] is True
    assert body["review"]["review_id"] == rid and body["latest_review_id"] is None
    assert body["intake"] is None


async def test_committed_without_alert_keeps_polling(env: Env) -> None:
    rid = await env.review("rv_pass", head="a" * 40, **COMMITTED)

    body = env.progress(rid)

    assert step(body, "deploy")["state"] == "running"
    assert body["final"] is False  # 대상 환경 배포 알림이 와야 끝


async def test_two_autofix_commits_chain(env: Env) -> None:
    first = await env.review("rv_1", head="a" * 40, verdict="pass", rounds=[{"round": 0}])
    second = await env.review("rv_2", head="b" * 40, requested_by="autofix:rv_1", verdict="pass", rounds=[{"round": 0}])
    third = await env.review("rv_3", head="c" * 40, requested_by="autofix:rv_2", status="waiting_ci", verdict="pass")
    await env.repo.update_review(first, status="superseded", superseded_by=second)
    await env.repo.update_review(second, status="superseded", superseded_by=third)

    old = env.progress(first)
    assert [c["review_id"] for c in old["chain"]] == [first, second, third]
    assert [c["autofix"] for c in old["chain"]] == [False, True, True]
    assert old["latest_review_id"] == third  # 화면이 최신 검토로 넘어간다
    assert old["final"] is False
    assert step(old, "review")["detail"] == "판정 pass · 수정 1회차 → 자동 수정 커밋으로 새 검토 rv_2"
    assert states(old)["ci"] == "skipped"

    latest = env.progress(third)
    assert [c["review_id"] for c in latest["chain"]] == [first, second, third]
    assert latest["latest_review_id"] is None
    assert step(latest, "review")["detail"] == "판정 pass · 자동 수정 커밋 2회"
    assert step(latest, "review")["at"] is None  # 0009 judged_at 이 없는 행 — 시각 없이
    assert states(latest) == {"review": "done", "ci": "running", "merge": "waiting", "gitops": "waiting",
                              "deploy": "waiting"}


def fix_snapshot(*, why: str = "SQLite 는 단일 replica 로 실행해야 합니다", value: int = 1) -> dict[str, Any]:
    return {
        "round": 0, "verdict": "fix", "reasons": [],
        "findings": [{"finding_id": "db_replicas", "rule_id": "DB-003", "severity": "high",
                      "title": "SQLite 다중 replica", "location": {"spec_path": "/runtime/replicas"},
                      "evidence": "replicas=2", "autofix": "allowed", "irreversible": False}],
        "items": [{"finding_id": "db_replicas", "why": why, "cited_rule_ids": ["DB-003"]}],
        "doc_ids": ["rules/db-003#0"],
        "patch": {"kind": "config", "target_finding_ids": ["db_replicas"],
                  "ops": [{"op": "replace", "path": "/runtime/replicas", "value": value}], "files": []},
    }


async def test_history_survives_autofix_pass_and_old_url(env: Env) -> None:
    snapshot = fix_snapshot()
    first = await env.review("rv_before", head="a" * 40, verdict="pass", findings=[], rounds=[snapshot])
    latest = await env.review("rv_after", head="b" * 40, requested_by="autofix:rv_before", **COMMITTED)
    await env.repo.update_review(first, status="superseded", superseded_by=latest)

    old, body = env.progress(first), env.progress(latest)

    assert old["latest_review_id"] == latest
    assert body["findings"] == [] and body["rounds"] == [] and body["review"]["verdict"] == "pass"
    history = body["history"]
    assert [h["review_id"] for h in history] == [first, latest]
    assert [h["is_current"] for h in history] == [False, True]
    before = history[0]
    assert before["rounds"][0]["items"] == snapshot["items"]
    assert before["rounds"][0]["findings"] == snapshot["findings"]
    assert before["rounds"][0]["doc_ids"] == snapshot["doc_ids"]
    assert before["rounds"][0]["ops"] == snapshot["patch"]["ops"]
    assert before["rounds"][0]["actor"] == "ai"
    assert before["patch_commit"] == "committed"
    assert before["next"] == {"review_id": latest, "kind": "autofix", "commit": "b" * 40,
                              "commit_url": f"https://github.com/{REPO}/commit/{'b' * 40}",
                              "status": "committed", "verdict": "pass"}
    assert old["history"][0]["rounds"] == before["rounds"]
    assert len(old["history"]) == 1  # 요청한 검토 뒤의 회차는 그 검토로 이동해 확인


async def test_history_preserves_repeated_finding_by_review_and_round(env: Env) -> None:
    one, two, three = fix_snapshot(why="첫 설명"), fix_snapshot(why="두 번째 설명"), fix_snapshot(why="다른 검토의 설명")
    two["round"] = 1
    first = await env.review("rv_1", head="a" * 40, rounds=[one, two])
    second = await env.review("rv_2", head="b" * 40, requested_by="autofix:rv_1", rounds=[three])
    await env.repo.update_review(first, status="superseded", superseded_by=second)

    history = env.progress(second)["history"]

    assert [[r["items"][0]["why"] for r in h["rounds"]] for h in history] == [
        ["첫 설명", "두 번째 설명"], ["다른 검토의 설명"]]
    assert [r["round"] for r in history[0]["rounds"]] == [0, 1]


@pytest.mark.parametrize("requested_by", ["hyeyeon", "autofix:rv_other"])
async def test_history_manual_or_wrong_autofix_parent_is_not_applied(env: Env, requested_by: str) -> None:
    first = await env.review("rv_before", head="a" * 40, rounds=[fix_snapshot()])
    latest = await env.review("rv_after", head="b" * 40, requested_by=requested_by, **COMMITTED)
    await env.repo.update_review(first, status="superseded", superseded_by=latest)

    before = env.progress(latest)["history"][0]

    assert before["patch_commit"] == "unconfirmed"
    assert before["next"]["kind"] == "new_commit"


async def test_history_failed_branch_update_is_not_committed(env: Env) -> None:
    first = await env.review("rv_before", head="a" * 40, status="failed", verdict="pass",
                             error="commit_fix: branch changed", rounds=[fix_snapshot()])
    # commit_fix 는 새 검토를 먼저 만든다. 브랜치 갱신 실패 시 superseded_by 는 연결되지 않는다.
    await env.review("rv_orphan", head="b" * 40, requested_by="autofix:rv_before", status="failed")

    history = env.progress(first)["history"]

    assert len(history) == 1
    assert history[0]["patch_commit"] == "failed" and history[0]["next"] is None


async def test_history_internal_patch_is_not_yet_a_commit(env: Env) -> None:
    rid = await env.review("rv_h", head="a" * 40, status="needs_human", verdict="needs_human",
                           rounds=[fix_snapshot()])
    assert env.progress(rid)["history"][0]["patch_commit"] == "unconfirmed"


async def test_history_human_round_and_public_fields(env: Env) -> None:
    snapshot = fix_snapshot()
    snapshot["human"] = {"decision": "approved", "approver": "혜연", "internal": "not-public"}
    snapshot["deploy_spec"] = {"private": "not-public"}
    snapshot["patch"]["files"] = [{"path": "private.yaml", "diff": "not-public"}]
    snapshot["patch"]["ops"].extend([
        {"op": "replace", "path": "/runtime/env/PASSWORD", "value": "ordinary-private-value"},
        {"op": "add", "path": "/runtime/env/NORMAL", "value": "another-private-value"},
        {"op": "replace", "path": "/runtime", "value": {"env": {"PASSWORD": "nested-private-value"}}},
    ])
    snapshot["items"][0]["llm"] = {"prompt": "not-public"}
    rid = await env.review("rv_h", head="a" * 40, verdict="pass", rounds=[snapshot],
                           decision={"items": [], "llm": {"input": "not-public"}},
                           final_spec={"runtime": {"env": {"PASSWORD": "not-public"}}})

    history = env.progress(rid)["history"]
    report = history[0]["rounds"][0]

    assert report["actor"] == "human" and report["approver"] == "혜연"
    assert report["ops"][0]["value"] == 1
    assert report["ops"][1]["value"] == MASK and report["ops"][2]["value"] == MASK
    assert report["ops"][3]["value"]["env"]["PASSWORD"] == MASK
    assert "not-public" not in str(history) and "private-value" not in str(history)
    assert "files" not in report and "llm" not in report["items"][0]
    assert "deploy_spec" not in report and "final_spec" not in history[0]


async def test_history_empty_legacy_records(env: Env) -> None:
    rid = await env.review("rv_legacy", head="a" * 40)
    report = env.progress(rid)["history"][0]
    assert report["rounds"] == [] and report["findings"] == [] and report["items"] == []
    assert report["patch_commit"] == "unconfirmed"


async def test_needs_human_waiting_then_approved(env: Env) -> None:
    rid = await env.review("rv_h", head="a" * 40, status="needs_human", verdict="needs_human",
                           reasons=["GENERATED_SPEC_UNVERIFIED"],
                           findings=[{"finding_id": "f1", "rule_id": "NET-001", "severity": "high", "title": "공개",
                                      "location": {"spec_path": "/network"}, "evidence": "public"}])

    waiting = env.progress(rid)
    assert states(waiting) == {"review": "done", "human": "running", "ci": "waiting", "merge": "waiting",
                               "gitops": "waiting", "deploy": "waiting"}
    assert step(waiting, "human")["detail"] == "사람 확인 대기 — GENERATED_SPEC_UNVERIFIED"
    assert waiting["review"]["reason_messages"]["GENERATED_SPEC_UNVERIFIED"]
    assert waiting["findings"][0]["rule_id"] == "NET-001"
    assert waiting["final"] is False

    op = {"op": "replace", "path": "/runtime/replicas", "value": 2}
    await env.repo.update_review(rid, status="merging", human_decision={
        "decision": "approved", "approver": "혜연", "use_recommendations": True,
        "edited_ops": [op, {"op": "replace", "path": "/network/public", "value": False}], "defaulted_ops": [op]})
    approved = env.progress(rid)
    assert step(approved, "human") | {"at": None} == {"key": "human", "label": "사람 확인", "state": "done",
                                                      "detail": "승인 — 혜연 · 권장값 1개 · 수정값 1개", "at": None}
    assert states(approved)["merge"] == "running"


async def test_rejected_stops_at_human(env: Env) -> None:
    rid = await env.review("rv_r", head="a" * 40, status="rejected", verdict="needs_human",
                           human_decision={"decision": "rejected", "approver": "찬건", "edited_ops": []})

    body = env.progress(rid)

    assert states(body) == {"review": "done", "human": "failed", "ci": "skipped", "merge": "skipped",
                            "gitops": "skipped", "deploy": "skipped"}
    assert step(body, "human")["detail"] == "거절 — 찬건"
    assert body["final"] is True


async def test_blocked_overlay_resource_removed(env: Env) -> None:
    reason = "OVERLAY_RESOURCE_REMOVED: ingress.yaml"
    rid = await env.review("rv_b", head="a" * 40, status="blocked", verdict="pass", error=reason,
                           deploy_result={"status": "blocked", "commit_sha": None, "reason": reason})

    body = env.progress(rid)

    assert states(body) == {"review": "done", "ci": "done", "merge": "skipped", "gitops": "failed",
                            "deploy": "skipped"}
    assert step(body, "gitops")["detail"] == reason
    assert step(body, "gitops")["at"] is not None
    assert card(body, "aws")["render"] == {"status": "blocked", "reason": reason, "gitops_commit_sha": None}
    assert body["links"]["merge_commit"] is None and body["links"]["gitops_commit"] is None
    assert body["final"] is True


async def test_failed_merge_stops_at_merge(env: Env) -> None:
    rid = await env.review("rv_f", head="a" * 40, status="failed", verdict="pass",
                           error="merge_pr: 커밋 상태 review-service/verify 를 쓰지 못해 병합하지 않는다")

    body = env.progress(rid)

    assert states(body) == {"review": "done", "ci": "done", "merge": "failed", "gitops": "skipped",
                            "deploy": "skipped"}
    assert step(body, "merge")["detail"].startswith("merge_pr:")


async def test_intake_generated_spec(env: Env) -> None:
    await env.repo.insert_intake(intake_id="in_1", repository=REPO, head_repository=REPO, pr_number=12,
                                 head_sha="9" * 40, head_ref="feat/x", path="deploy.yaml", kind="missing", errors=[],
                                 requested_by="hyeyeon")
    await env.repo.finish_intake("in_1", status="generated", reason="GENERATED")
    await env.repo.link_intake("in_1", result_commit_sha="a" * 40)
    rid = await env.review("rv_g", head="a" * 40, status="reviewing")
    await env.repo.link_intake("in_1", review_id=rid)

    body = env.progress(rid)

    assert body["intake"]["kind"] == "missing" and body["intake"]["status"] == "generated"
    assert body["intake"]["result_commit_sha"] == "a" * 40 and body["intake"]["intake_id"] == "in_1"
    assert [s["key"] for s in body["steps"]][:2] == ["intake", "review"]
    assert step(body, "intake")["state"] == "done"
    assert step(body, "intake")["detail"] == "deploy.yaml 없음 → 명세 생성 · 커밋 aaaaaaa"
    assert step(body, "review")["state"] == "running"


async def test_intake_found_through_chain(env: Env) -> None:
    """intake 는 첫 검토에 이어진다 — 자동 수정 뒤 검토에서도 보인다."""
    await env.repo.insert_intake(intake_id="in_1", repository=REPO, head_repository=REPO, pr_number=12,
                                 head_sha="9" * 40, head_ref="feat/x", path="deploy.yaml", kind="empty", errors=[],
                                 requested_by="hyeyeon")
    first = await env.review("rv_1", head="a" * 40)
    await env.repo.link_intake("in_1", review_id=first)
    second = await env.review("rv_2", head="b" * 40, requested_by="autofix:rv_1")
    await env.repo.update_review(first, status="superseded", superseded_by=second)

    assert env.progress(second)["intake"]["intake_id"] == "in_1"


async def test_only_local_alert_arrived(env: Env) -> None:
    rid = await env.review("rv_l", head="a" * 40, **COMMITTED)
    recorded = await handle_deploy_event(env.repo, aws_event(env_name="local"))
    assert recorded["cross_env"] is True

    body = env.progress(rid)

    assert card(body, "local")["deploy"]["kind"] == "healthy"
    assert "render" not in card(body, "local")  # 렌더 결과는 대상 환경만
    assert card(body, "aws")["deploy"] is None
    assert step(body, "deploy")["state"] == "done"  # 하나라도 healthy 면
    assert step(body, "deploy")["detail"] == "aws 알림 대기 · local Healthy"
    assert body["final"] is False  # 대상 환경(aws) 알림은 아직


async def test_degraded_deploy_fails_step(env: Env) -> None:
    rid = await env.review("rv_d", head="a" * 40, **COMMITTED)
    await env.repo.add_deploy_event(review_id=rid, app="sample-app", target_env="aws", kind="degraded",
                                    image_tag=MERGE[:7], payload={})

    body = env.progress(rid)

    assert step(body, "deploy")["state"] == "failed"
    assert body["final"] is True


async def test_done_steps_use_stage_time_columns(env: Env) -> None:
    """7차 0009 열(judged_at·human_decided_at·merged_at·gitops_committed_at)이 있으면 끝난 단계 시각으로 쓴다."""
    rid = await env.review("rv_t", head="a" * 40, **COMMITTED,
                           human_decision={"decision": "approved", "approver": "혜연", "edited_ops": []})
    times = {"judged_at": "2026-10-09T10:00:00+00:00", "human_decided_at": "2026-10-09T10:05:00+00:00",
             "merged_at": "2026-10-09T10:07:00+00:00", "gitops_committed_at": "2026-10-09T10:08:00+00:00"}
    await env.repo.update_review(rid, **{k: datetime.fromisoformat(v) for k, v in times.items()})

    body = env.progress(rid)

    at = {s["key"]: s["at"] for s in body["steps"]}
    assert {k: at[k] for k in ("review", "human", "merge", "gitops")} == {
        "review": "2026-10-09T10:00:00Z", "human": "2026-10-09T10:05:00Z", "merge": "2026-10-09T10:07:00Z",
        "gitops": "2026-10-09T10:08:00Z"}
    assert at["ci"] is None


def test_progress_404(env: Env) -> None:
    assert env.client.get("/reviews/rv_none/progress").status_code == 404


async def test_closed_pr_superseded_is_final(env: Env) -> None:
    rid = await env.review("rv_c", head="a" * 40, status="needs_human", verdict="needs_human")
    await env.repo.supersede_open(repository=REPO, pr_number=12, superseded_by=None, error="PR closed")

    body = env.progress(rid)

    assert body["latest_review_id"] is None
    assert step(body, "review")["detail"] == "PR 이 병합 없이 닫혔다"
    assert body["final"] is True


def test_ui_pages_served_without_token(env: Env) -> None:
    for path in ("/ui/reviews", "/ui/reviews/rv_x"):
        resp = env.client.get(path)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "connect-src 'self'" in resp.headers["content-security-policy"]
        assert "/progress" in resp.text


def test_app_urls_only_http() -> None:
    raw = ('{"sample-app": {"aws": "http://a.example", "local": "javascript:alert(1)"},'
           ' "other": "x", "web": {"gcp": "https://w.example/path"}}')
    assert parse_app_urls(raw) == {"sample-app": {"aws": "http://a.example"}, "web": {"gcp": "https://w.example/path"}}
    assert parse_app_urls("not json") == {}
    assert parse_app_urls(None) == {}


def test_settings_from_env() -> None:
    settings = ProgressSettings.from_env({"DEPLOY_ENVS": "aws, local ,aws", "PLANNED_ENVS": ""})
    assert settings.deploy_envs == ("aws", "local")
    assert settings.planned_envs == ()
    assert ProgressSettings.from_env({}).planned_envs == ("gcp",)


async def test_history_preserves_chunk_evidence_and_legacy_items(env: Env) -> None:
    evidence = {"chunk_id": "rules/any/STO-003.md#0", "rule_id": "STO-003", "doc_type": "rule",
                "source_uri": "rules/any/STO-003.md", "excerpt": "공개 버킷을 비공개로 유지합니다.",
                "content_hash": "a" * 64, "truncated": False}
    finding = {"finding_id": "bucket", "rule_id": "STO-003", "severity": "high", "title": "공개 버킷",
               "location": {"spec_path": "/storage/buckets/0/public"}}
    current = {"finding_id": "bucket", "why": "규칙에 따라 공개를 끕니다.", "cited_rule_ids": ["STO-003"],
               "cited_chunk_ids": [evidence["chunk_id"]], "evidence": [evidence], "citation_status": "chunk_verified"}
    rid = await env.review("rv_evidence", head="a" * 40, status="needs_human", verdict="needs_human",
                           findings=[finding], decision={"items": [current]},
                           rounds=[{"round": 0, "findings": [finding], "items": [current], "verdict": "fix",
                                    "patch_source": "deterministic", "doc_ids": [evidence["chunk_id"]]}])
    report = env.progress(rid)["history"][0]
    assert report["items"][0]["evidence"] == [evidence]
    assert report["rounds"][0]["patch_source"] == "deterministic"
    legacy = {"finding_id": "bucket", "why": "기존 설명", "cited_rule_ids": ["STO-003"]}
    await env.repo.update_review(rid, decision={"items": [legacy]}, rounds=[])
    assert env.progress(rid)["history"][0]["items"] == [legacy]
