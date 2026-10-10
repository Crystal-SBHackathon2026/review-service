"""진행 화면 — GET /reviews/{review_id}/progress. 화면(static/progress.html)은 이것 하나만 폴링한다.

검토 한 건 = 대상 환경 하나다 (deploy.yaml). 단계 시각은 아는 것만 준다:
    intake  spec_intakes.created_at
    끝난 단계  reviews 0009 열 — review judged_at · human human_decided_at · merge merged_at · gitops gitops_committed_at
    지금 단계·멈춘 단계  reviews.updated_at
    deploy  deploy_events.received_at (가장 먼저 온 알림)
0009 전 행과 ci 는 null (화면은 시각 없이 그린다).

단계 상태는 reviews.status 에서 계산한다. rejected·failed·blocked 는 그 단계에서 failed 로 멈추고 뒤는 skipped.
failed 는 어느 단계에서 났는지 저장하지 않아서 merge_sha·error 앞부분(워커 _fail 의 "merge_pr:"·"CI ...")으로 고른다.

환경 카드: DEPLOY_ENVS(실제, 기본 aws,local)와 PLANNED_ENVS(계획, 기본 gcp). 대상 환경은 렌더 결과(deploy_result)까지,
다른 실제 환경은 배포 알림만 (review_api.argocd 의 cross_env 기록). 앱 주소는 APP_URLS (JSON {app: {env: url}}).

Degraded 중 Argo Rollouts 자동 중단(카나리 분석 실패·progressDeadlineAbort)은 알림 payload 로 가른다 (rollout_abort).
대상 환경만 실패하고 다른 환경은 Healthy 면 배포 단계는 partial (부분 완료).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from review_ai.masking import mask_spec
from review_ai.secrets_pattern import MASK, looks_secret
from review_ai.state import REASON_MESSAGES
from review_common.github import DEFAULT_GITOPS_REPO
from review_common.repository import ReviewRepository

log = logging.getLogger(__name__)

REVIEW_KEYS = ("review_id", "app", "target_env", "spec_ref", "status", "verdict", "reasons", "findings", "decision",
               "rounds", "human_decision", "deploy_result", "merge_sha", "gitops_commit_sha", "error",
               "superseded_by", "requested_by", "created_at", "updated_at", "pr_number",
               "judged_at", "human_decided_at", "merged_at", "gitops_committed_at")  # 단계 시각 0009
MAX_CHAIN = 20  # 같은 PR 검토 이력을 따라가는 상한 — 자동 수정은 한 번이면 LOOP_EXHAUSTED 라 실제로는 몇 개뿐이다
AUTOFIX_PREFIX = "autofix:"  # 워커 commit_fix 가 넘긴 검토의 requested_by
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
STOPPED = frozenset({"rejected", "failed", "blocked"})

LABELS = {"intake": "명세 생성", "review": "AI 검토", "human": "사람 확인", "ci": "CI 확인", "merge": "병합",
          "gitops": "overlay 커밋", "deploy": "배포"}
INTAKE_KINDS = {"missing": "deploy.yaml 없음", "empty": "빈 deploy.yaml", "yaml_error": "YAML 형식 오류",
                "schema_error": "명세 형식 오류"}
INTAKE_STATUSES = {"processing": "처리 중", "generated": "명세 생성", "repaired": "형식 복구", "rejected": "거절",
                   "failed": "실패"}
HEALTH = {"healthy": "Healthy", "degraded": "Degraded", "sync_failed": "SyncFailed"}
STAGE_AT = {"review": "judged_at", "human": "human_decided_at", "merge": "merged_at",
            "gitops": "gitops_committed_at"}  # 끝난 단계의 시각 (0009). 그 전 행은 None


def review_view(row: dict[str, Any]) -> dict[str, Any]:
    """GET /reviews/{id} 응답 — 진행 화면의 review 도 같은 모양."""
    reasons = row.get("reasons") or []
    return {**{k: row.get(k) for k in REVIEW_KEYS},
            "reason_messages": {code: REASON_MESSAGES[code] for code in reasons if code in REASON_MESSAGES}}


async def deployment_summary(repo, row):
    events = [e for e in await repo.list_deployments(row["review_id"], limit=50)
              if e["target_env"] == row["target_env"]]
    failed = await repo.is_deployment_failing(row["review_id"])  # 마지막 알림 기준 — 실패 뒤 회복하면 성공 쪽을 본다
    latest = next((e for e in events if (e["kind"] != "deployed") == failed), events[0] if events else None)
    verified_success = (latest is not None and latest["kind"] == "deployed"
                        and latest["payload"].get("health") == "Healthy"
                        and latest["payload"].get("sync_status") == "Synced"
                        and (latest["payload"].get("operation") or {}).get("phase") == "Succeeded")
    return {"status": "failed" if failed else "healthy" if verified_success else "unknown",
            "event_id": latest["event_id"] if latest else None,
            "analysis_status": latest["analysis_status"] if latest else None}


def _envs(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    return tuple(dict.fromkeys(e.strip() for e in raw.split(",") if e.strip()))


def parse_app_urls(raw: str | None) -> dict[str, dict[str, str]]:
    """APP_URLS — {"sample-app": {"aws": "http://…", "local": "http://…"}}. http(s) 주소만 받고 나머지는 버린다.

    화면이 그대로 링크로 그리므로 javascript: 같은 주소가 들어가지 않게 여기서 거른다. 깨진 JSON 은 경고하고 빈 값."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("APP_URLS 가 JSON 이 아니다 — 앱 주소 없이 시작")
        return {}
    if not isinstance(data, dict):
        log.warning("APP_URLS 는 {app: {env: url}} 이어야 한다 — 앱 주소 없이 시작")
        return {}
    urls: dict[str, dict[str, str]] = {}
    for app, envs in data.items():
        if not isinstance(envs, dict):
            continue
        good = {env: url for env, url in envs.items()
                if isinstance(url, str) and re.match(r"^https?://[^\s\"'<>]+$", url)}
        if len(good) != len(envs):
            log.warning("APP_URLS %s: http(s) 주소가 아닌 값은 버렸다", app)
        if good:
            urls[str(app)] = good
    return urls


@dataclass(frozen=True)
class ProgressSettings:
    deploy_envs: tuple[str, ...] = ("aws", "local")  # DEPLOY_ENVS — 배포 알림이 오는 실제 환경
    planned_envs: tuple[str, ...] = ("gcp",)          # PLANNED_ENVS — 화면에 "계획"으로만 보이는 환경
    app_urls: Mapping[str, Mapping[str, str]] = field(default_factory=dict)  # APP_URLS
    gitops_repo: str = DEFAULT_GITOPS_REPO            # GITOPS_REPO — overlay 커밋 링크

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> ProgressSettings:
        return cls(deploy_envs=_envs(environ.get("DEPLOY_ENVS"), cls.deploy_envs),
                   planned_envs=_envs(environ.get("PLANNED_ENVS"), cls.planned_envs),
                   app_urls=parse_app_urls(environ.get("APP_URLS")),
                   gitops_repo=environ.get("GITOPS_REPO") or DEFAULT_GITOPS_REPO)


# --- 이력 -----------------------------------------------------------------------------------------

async def review_chain(repo: ReviewRepository, row: dict[str, Any]) -> list[dict[str, Any]]:
    """같은 PR 검토 이력 — superseded_by 를 앞뒤로 따라간다. 오래된 것부터."""
    chain, seen = [row], {row["review_id"]}
    current = row
    while len(chain) < MAX_CHAIN:
        parent = await repo.find_superseding_parent(current["review_id"])
        if parent is None or parent["review_id"] in seen:
            break
        chain.insert(0, parent)
        seen.add(parent["review_id"])
        current = parent
    current = row
    while current.get("superseded_by") and len(chain) < MAX_CHAIN:
        nxt = await repo.get_review(current["superseded_by"])
        if nxt is None or nxt["review_id"] in seen:
            break
        chain.append(nxt)
        seen.add(nxt["review_id"])
        current = nxt
    return chain


def _is_autofix(row: dict[str, Any]) -> bool:
    return str(row.get("requested_by") or "").startswith(AUTOFIX_PREFIX)


def _commit_link(row: dict[str, Any]) -> str | None:
    ref = row.get("spec_ref") or {}
    repository, commit = ref.get("repository") or "", ref.get("commit") or ""
    if REPO_NAME.fullmatch(repository) and SHA.fullmatch(commit):
        return f"https://github.com/{repository}/commit/{commit}"
    return None


def _history_ops(patch: dict[str, Any]) -> list[dict[str, Any]]:
    """공개 이력에 overlay diff·env 원문 값을 내보내지 않는다. 기존 비밀 값 판정을 재사용한다."""
    ops = []
    for op in patch.get("ops") or []:
        view = {k: op[k] for k in ("op", "path", "value") if k in op}
        # 사람 편집도 rounds 에 들어온다. JSON Pointer 의 env 아래 값은 이름과 무관하게 가린다.
        parts = str(op.get("path") or "").split("/")[1:]
        parts = [p.replace("~1", "/").replace("~0", "~") for p in parts]
        if "value" in view and ("env" in parts or any(looks_secret(p, view["value"]) for p in parts)):
            view["value"] = MASK
        ops.append(mask_spec(view))
    return ops


def _history_report(report: dict[str, Any]) -> dict[str, Any]:
    """화면에 필요한 설명만 고른다. 명세 전체·LLM 내부 정보·files.diff 는 반환하지 않는다."""
    findings = [{k: f[k] for k in ("finding_id", "rule_id", "severity", "title", "location", "evidence",
                                  "irreversible", "autofix") if k in f}
                for f in report.get("findings") or []]
    for finding in findings:
        finding["location"] = {"spec_path": (finding.get("location") or {}).get("spec_path")}
    items = [{k: item[k] for k in ("finding_id", "why", "cited_rule_ids") if k in item}
             for item in report.get("items") or []]
    return mask_spec({"findings": findings, "items": items, "doc_ids": report.get("doc_ids") or []})


def review_history(chain: list[dict[str, Any]], row: dict[str, Any]) -> list[dict[str, Any]]:
    """요청한 검토까지의 이력. 현재 findings 와 섞지 않고 커밋 반영은 성공한 연결로만 확인한다."""
    history = []
    upto = chain[:chain.index(row) + 1]
    for i, r in enumerate(upto):
        nxt = chain[i + 1] if i + 1 < len(chain) else None
        linked = bool(nxt and r.get("superseded_by") == nxt["review_id"])
        autofix = bool(linked and r["status"] == "superseded"
                       and nxt.get("requested_by") == AUTOFIX_PREFIX + r["review_id"]
                       and r.get("app") == nxt.get("app") and r.get("target_env") == nxt.get("target_env")
                       and r.get("pr_number") == nxt.get("pr_number")
                       and (r.get("spec_ref") or {}).get("repository") == (nxt.get("spec_ref") or {}).get("repository")
                       and _commit_link(nxt))
        rounds = []
        for snapshot in r.get("rounds") or []:
            human = snapshot.get("human")
            rounds.append({"round": snapshot.get("round"), "verdict": snapshot.get("verdict"),
                           **_history_report(snapshot), "ops": _history_ops(snapshot.get("patch") or {}),
                           "actor": "human" if human else "ai",
                           "approver": human.get("approver") if human else None})
        history.append({
            "review_id": r["review_id"], "created_at": r.get("created_at"), "status": r["status"],
            "verdict": r.get("verdict"), "is_current": r["review_id"] == row["review_id"],
            "commit": (r.get("spec_ref") or {}).get("commit"), "commit_url": _commit_link(r),
            **_history_report({"findings": r.get("findings"), "items": (r.get("decision") or {}).get("items")}),
            "rounds": rounds,
            "patch_commit": "committed" if autofix else "failed" if r["status"] == "failed"
                            and str(r.get("error") or "").startswith("commit_fix:") else "unconfirmed",
            "next": ({"review_id": nxt["review_id"], "kind": "autofix" if autofix else "new_commit",
                      "commit": (nxt.get("spec_ref") or {}).get("commit"), "commit_url": _commit_link(nxt),
                      "status": nxt["status"], "verdict": nxt.get("verdict")} if linked else None),
        })
    return history


# --- 환경 -----------------------------------------------------------------------------------------

def _image_tag(image: Any) -> str | None:
    last = str(image).split("@", 1)[0].rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else None


def rollout_abort(event: dict[str, Any]) -> dict[str, Any] | None:
    """Degraded 알림이 Argo Rollouts 자동 중단인지. 중단이면 {"serving_tag": 지금 서비스 중인 이전 버전 태그 | None}.

    알림 payload 의 resources 는 Application status.resources 다. Argo CD(v3.5.4) Rollout 헬스 검사는 Rollout 의
    status.phase·message 를 그대로 올린다 — 중단이면 health {status: Degraded, message: "RolloutAborted: Rollout aborted
    update to revision N: …"}. Rollout 이 Degraded 여도 message 에 abort 가 없으면(형식 오류 등) 중단으로 보지 않는다.
    이전 버전 태그는 images 중 이 검토의 태그(image_tag, 병합 SHA)가 아닌 것 — 중단 직후엔 새 태그도 같이 올 수 있다."""
    payload = event.get("payload") or {}
    if event.get("kind") != "degraded":
        return None
    messages = [str((r.get("health") or {}).get("message") or "") for r in payload.get("resources") or []
                if isinstance(r, dict) and r.get("kind") == "Rollout"
                and (r.get("health") or {}).get("status") == "Degraded"]
    if not any("abort" in m.lower() for m in messages):
        return None
    mine = str(event.get("image_tag") or "").lower()
    others = [t for t in map(_image_tag, payload.get("images") or [])
              if t and not (mine and (mine.startswith(t.lower()) or t.lower().startswith(mine)))]
    return {"serving_tag": others[0] if others else None}


def env_cards(row: dict[str, Any], events: list[dict[str, Any]], settings: ProgressSettings) -> list[dict[str, Any]]:
    target = row["target_env"]
    latest: dict[str, dict[str, Any]] = {}
    for event in events:  # received_at 순 — 마지막 알림이 지금 상태
        latest[event["target_env"]] = event
    actual = list(dict.fromkeys([*settings.deploy_envs, target, *latest]))
    planned = [env for env in settings.planned_envs if env not in actual]
    urls = settings.app_urls.get(row["app"]) or {}
    cards: list[dict[str, Any]] = []
    for env in actual:
        event = latest.get(env)
        deploy = ({"kind": event["kind"], "image_tag": event["image_tag"], "received_at": event["received_at"]}
                  if event else None)
        abort = rollout_abort(event) if event else None
        if deploy is not None and abort is not None:
            deploy.update(rollout_aborted=True, serving_tag=abort["serving_tag"])
        card: dict[str, Any] = {"env": env, "is_target": env == target, "deploy": deploy, "app_url": urls.get(env)}
        if env == target:
            result = row.get("deploy_result") or {}
            card["render"] = ({"status": result.get("status"), "reason": result.get("reason"),
                               "gitops_commit_sha": row.get("gitops_commit_sha") or result.get("commit_sha")}
                              if result else None)
        cards.append(card)
    cards.extend({"env": env, "planned": True} for env in planned)
    return cards


# --- 단계 -----------------------------------------------------------------------------------------

def _short(sha: str | None) -> str:
    return (sha or "")[:7]


def _failed_stage(row: dict[str, Any]) -> str:
    """failed 가 난 단계. 워커 _fail 의 error 앞부분으로 고른다 — 저장된 단계 정보가 없다."""
    error = row.get("error") or ""
    if row.get("merge_sha"):
        return "gitops"  # 병합은 됐다 — gitops 커밋 재시도까지 실패
    if error.startswith("merge_pr"):
        return "merge"
    if error.startswith("CI "):
        return "ci"
    return "review"


def _human_detail(row: dict[str, Any]) -> str:
    human = row.get("human_decision") or {}
    if not human:
        reasons = ", ".join(row.get("reasons") or []) or "사유 없음"
        return f"사람 확인 대기 — {reasons}"
    who = human.get("approver") or "-"
    if human.get("decision") == "rejected":
        return f"거절 — {who}"
    defaulted = len(human.get("defaulted_ops") or [])
    edited = len(human.get("edited_ops") or []) - defaulted
    parts = [f"승인 — {who}"]
    if defaulted:
        parts.append(f"권장값 {defaulted}개")
    if edited > 0:
        parts.append(f"수정값 {edited}개")
    if not defaulted and edited <= 0:
        parts.append("작성한 명세 그대로")
    return " · ".join(parts)


def _deploy_state(cards: list[dict[str, Any]]) -> tuple[str, str, Any]:
    """대상 환경이 실패하면 failed — 다른 환경이 healthy 면 partial(부분 완료). 대상이 실패가 아니면
    envs 중 하나라도 healthy 면 done, (healthy 없이) degraded 면 failed, 알림이 없으면 running."""
    real = [c for c in cards if not c.get("planned")]
    parts, healthy, degraded = [], [], []
    for card in real:
        deploy = card["deploy"]
        if deploy is None:
            parts.append(f"{card['env']} 알림 대기")
            continue
        label = "자동 중단 → 이전 버전 유지" if deploy.get("rollout_aborted") else HEALTH.get(deploy["kind"], deploy["kind"])
        parts.append(f"{card['env']} {label}")
        (healthy if deploy["kind"] == "healthy" else degraded).append(deploy["received_at"])
    detail = " · ".join(parts)
    target_failure = next((c["deploy"] for c in real if c.get("is_target") and c.get("deploy")
                           and c["deploy"]["kind"] in {"degraded", "sync_failed"}), None)
    if target_failure:
        others_healthy = any(not c.get("is_target") and c.get("deploy") and c["deploy"]["kind"] == "healthy"
                             for c in real)
        return "partial" if others_healthy else "failed", detail, target_failure["received_at"]
    if healthy:
        return "done", detail, min(healthy)
    if degraded:
        return "failed", detail, min(degraded)
    return "running", detail, None


def build_steps(row: dict[str, Any], chain: list[dict[str, Any]], intake: dict[str, Any] | None,
                cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status, error = row["status"], row.get("error") or ""
    human = row.get("human_decision")
    upto = chain[:chain.index(row) + 1] if row in chain else [row]
    autofixes = sum(1 for r in upto if _is_autofix(r))
    rounds = len(row.get("rounds") or [])
    result = row.get("deploy_result") or {}

    keys = ["review"]
    if intake is not None:
        keys.insert(0, "intake")
    if human or status in ("needs_human", "rejected") or row.get("verdict") == "needs_human":
        keys.append("human")
    keys += ["ci", "merge", "gitops", "deploy"]

    # 지금(또는 멈춘) 단계와 그 상태
    deploy_state, deploy_detail, deploy_at = _deploy_state(cards)
    stage, outcome = {
        "received": ("review", "running"), "reviewing": ("review", "running"),
        "needs_human": ("human", "running"), "waiting_ci": ("ci", "running"), "merging": ("merge", "running"),
        "committed": ("deploy", deploy_state), "blocked": ("gitops", "failed"), "rejected": ("human", "failed"),
        "superseded": ("review", "superseded"),
    }.get(status) or (_failed_stage(row), "failed")
    if stage not in keys:  # needs_human 없이 rejected 등 — 안전장치
        stage = "review"

    details: dict[str, str] = {
        "review": " · ".join(filter(None, [
            f"판정 {row['verdict']}" if row.get("verdict") else "AI 검토 중",
            f"수정 {rounds}회차" if rounds else "",
            f"자동 수정 커밋 {autofixes}회" if autofixes else "",
        ])),
        "human": _human_detail(row),
        "ci": "CI 결과 대기",
        "merge": f"병합 {_short(row.get('merge_sha'))}" if row.get("merge_sha") else "병합 대기",
        "gitops": (f"overlay 커밋 {_short(row.get('gitops_commit_sha') or result.get('commit_sha'))}"
                   if result.get("status") == "committed" else "gitops 커밋 대기"),
        "deploy": deploy_detail,
    }
    if intake is not None:
        kind = INTAKE_KINDS.get(intake["kind"], intake["kind"])
        made = f" · 커밋 {_short(intake.get('result_commit_sha'))}" if intake.get("result_commit_sha") else ""
        details["intake"] = f"{kind} → {INTAKE_STATUSES.get(intake['status'], intake['status'])}{made}"

    stage_index = keys.index(stage)
    steps = []
    for i, key in enumerate(keys):
        at: Any = None
        if i < stage_index:
            state = "done"
            at = row.get(STAGE_AT.get(key, ""))
        elif i > stage_index:
            state = "waiting" if outcome == "running" else "skipped" if outcome in ("failed", "superseded") else "done"
        else:
            state = "done" if outcome == "superseded" and row.get("verdict") else outcome
            if state == "superseded":
                state = "skipped"
            at = deploy_at if key == "deploy" else row.get("updated_at")
        if key == "ci" and i < stage_index:
            details["ci"] = "CI 통과"
        if key == "merge" and i < stage_index and not row.get("merge_sha"):
            state, details["merge"] = "skipped", "병합하지 않음 (병합 전 overlay 검사에서 막힘)"
        if key == "intake":
            at = intake["created_at"] if intake else None
            if intake and intake["status"] == "processing":
                state = "running"
        elif key == "gitops" and state == "done":
            at = row.get("gitops_committed_at")
        steps.append({"key": key, "label": LABELS[key], "state": state, "detail": details[key], "at": at})

    # 멈춘 단계의 설명은 멈춘 이유로
    current = steps[stage_index]
    if status == "blocked":
        current["detail"] = result.get("reason") or error or "blocked"
    elif status == "failed":
        current["detail"] = error[:300] or "실패"
    elif status == "superseded":
        nxt = chain[chain.index(row) + 1] if row in chain and chain.index(row) + 1 < len(chain) else None
        if nxt is not None:
            how = "자동 수정 커밋" if _is_autofix(nxt) else "새 커밋"
            current["detail"] = f"{details['review']} → {how}으로 새 검토 {nxt['review_id']}"
        elif error == "PR closed":
            current["detail"] = "PR 이 병합 없이 닫혔다"
        else:
            current["detail"] = "새 커밋으로 넘어갔다 (명세 생성·형식 복구 등)"
    return steps


# --- 응답 -----------------------------------------------------------------------------------------

def links(row: dict[str, Any], settings: ProgressSettings) -> dict[str, str | None]:
    repository = (row.get("spec_ref") or {}).get("repository") or ""
    valid_repo = bool(REPO_NAME.match(repository))
    merge_sha, gitops_sha = row.get("merge_sha") or "", row.get("gitops_commit_sha") or ""
    return {
        "pr": f"https://github.com/{repository}/pull/{row['pr_number']}"
              if valid_repo and isinstance(row.get("pr_number"), int) else None,
        "merge_commit": f"https://github.com/{repository}/commit/{merge_sha}"
                        if valid_repo and SHA.match(merge_sha) else None,
        "gitops_commit": f"https://github.com/{settings.gitops_repo}/commit/{gitops_sha}"
                         if REPO_NAME.match(settings.gitops_repo) and SHA.match(gitops_sha) else None,
    }


def is_final(row: dict[str, Any], cards: list[dict[str, Any]], latest_review_id: str | None) -> bool:
    """화면이 폴링을 늦출 상태 — 더 바뀔 일이 (거의) 없다."""
    status = row["status"]
    if status in STOPPED:
        return True
    if status == "committed":
        return any(c.get("is_target") and c.get("deploy") for c in cards)
    return status == "superseded" and latest_review_id is None


async def build_progress(repo: ReviewRepository, review_id: str, settings: ProgressSettings) -> dict[str, Any] | None:
    row = await repo.get_review(review_id)
    if row is None:
        return None
    chain = await review_chain(repo, row)
    tail = chain[-1]
    latest_review_id = tail["review_id"] if row["status"] == "superseded" and tail["review_id"] != review_id else None
    intake_row = await repo.find_intake_by_reviews([r["review_id"] for r in chain])
    intake = ({k: intake_row.get(k) for k in ("intake_id", "kind", "status", "reason", "result_commit_sha",
                                               "created_at")} if intake_row else None)
    cards = env_cards(row, await repo.deploy_events_for(review_id), settings)
    return {
        "review": {**review_view(row), "deployment": await deployment_summary(repo, row),
                   "case_advice_status": (row.get("case_advice") or {}).get("status", "not_checked")},
        "chain": [{"review_id": r["review_id"], "status": r["status"], "verdict": r["verdict"],
                   "created_at": r["created_at"], "autofix": _is_autofix(r)} for r in chain],
        "history": review_history(chain, row),
        "latest_review_id": latest_review_id,
        "intake": intake,
        "steps": build_steps(row, chain, intake, cards),
        "envs": cards,
        "links": links(row, settings),
        "findings": row.get("findings"),
        "decision": row.get("decision"),
        "rounds": row.get("rounds"),
        "final": is_final(row, cards, latest_review_id),
    }
