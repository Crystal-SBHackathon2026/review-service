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
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

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
HEALTH = {"healthy": "Healthy", "degraded": "Degraded"}
STAGE_AT = {"review": "judged_at", "human": "human_decided_at", "merge": "merged_at",
            "gitops": "gitops_committed_at"}  # 끝난 단계의 시각 (0009). 그 전 행은 None


def review_view(row: dict[str, Any]) -> dict[str, Any]:
    """GET /reviews/{id} 응답 — 진행 화면의 review 도 같은 모양."""
    reasons = row.get("reasons") or []
    return {**{k: row.get(k) for k in REVIEW_KEYS},
            "reason_messages": {code: REASON_MESSAGES[code] for code in reasons if code in REASON_MESSAGES}}


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


# --- 환경 -----------------------------------------------------------------------------------------

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
        card: dict[str, Any] = {
            "env": env, "is_target": env == target,
            "deploy": ({"kind": event["kind"], "image_tag": event["image_tag"], "received_at": event["received_at"]}
                       if event else None),
            "app_url": urls.get(env),
        }
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
    """envs 중 하나라도 healthy 면 done, (healthy 없이) degraded 면 failed, 알림이 없으면 running."""
    real = [c for c in cards if not c.get("planned")]
    parts, healthy, degraded = [], [], []
    for card in real:
        deploy = card["deploy"]
        if deploy is None:
            parts.append(f"{card['env']} 알림 대기")
            continue
        parts.append(f"{card['env']} {HEALTH.get(deploy['kind'], deploy['kind'])}")
        (healthy if deploy["kind"] == "healthy" else degraded).append(deploy["received_at"])
    detail = " · ".join(parts)
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
        "review": review_view(row),
        "chain": [{"review_id": r["review_id"], "status": r["status"], "verdict": r["verdict"],
                   "created_at": r["created_at"], "autofix": _is_autofix(r)} for r in chain],
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
