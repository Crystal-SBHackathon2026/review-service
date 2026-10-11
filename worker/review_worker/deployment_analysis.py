"""Isolated failure analysis handler. Has no deployment or GitHub write dependency."""
from __future__ import annotations

import asyncio
import logging

from pydantic import ValidationError

from review_ai.deployment_analysis import diagnose, evidence_from
from review_ai.errors import TransientError
from review_ai.judge.llm import LlmUnavailable
from review_ai.spec.deploy_spec import DeploySpec
from review_ai.static_check import run_static_check
from review_ai.retrieval import Scope
from review_common.deployment import AnalysisRequested, MAX_ATTEMPTS

log = logging.getLogger(__name__)


class DeploymentAnalysisHandler:
    def __init__(self, repo, llm=None, retriever=None):
        self.repo, self.llm, self.retriever = repo, llm, retriever

    async def handle(self, value):
        try:
            msg = AnalysisRequested.model_validate_json(value)
        except ValidationError:
            return  # Do not print message payloads or validation errors.
        job = await self.repo.claim_analysis(msg.event_id)
        if not job:
            return
        event = await self.repo.get_deployment(msg.event_id)
        if event is None:
            raise RuntimeError("claimed deployment event missing")
        log.info("배포 분석 시작 event_id=%s kind=%s app=%s env=%s review_id=%s linked=%s attempt=%s",
                 msg.event_id, event["kind"], event["app"], event["target_env"], event["review_id"],
                 event["review_id"] is not None, job["attempts"])
        cases, docs = [], []
        if event["repository"] and event["spec_snapshot"]:
            cases = await self.repo.find_failure_cases(app=event["app"], repository=event["repository"],
                                                       target_env=event["target_env"])
            cases = [c for c in cases if c["case_id"] != msg.event_id]
            if self.retriever:
                try:
                    spec = DeploySpec.model_validate(event["spec_snapshot"])
                    found = await asyncio.wait_for(self.retriever.search(run_static_check(spec), event["target_env"],
                        Scope(app=event["app"], repository=event["repository"])), timeout=5)
                    docs = [dict(chunk_id=d["chunk_id"], text=d["text"][:4000]) for d in found[:5]]
                except Exception:
                    docs = []  # Current runtime evidence remains primary if auxiliary guides are unavailable.
        fallback = dict(summary=" 배포 실패의 원인을 확정하지 못했습니다.", hypotheses=[], spec_paths=[],
                        evidence=evidence_from(event["payload"]), additional_information=["관측 오류를 확인해 주세요."])
        try:
            status, result = await asyncio.wait_for(diagnose(event, self.llm, cases, docs), timeout=100)
            code = None
        except LlmUnavailable:
            status, result, code = "insufficient", fallback, "LLM_UNAVAILABLE"
        except (TransientError, TimeoutError):
            status = "pending" if job["attempts"] < MAX_ATTEMPTS else "failed"
            result, code = fallback, "ANALYSIS_TRANSIENT" if status == "pending" else "RETRIES_EXHAUSTED"
        except Exception as e:  # 원인을 남기지 않으면 fallback 결과만 보고 왜 무효였는지 알 수 없다
            log.warning("배포 분석 결과 무효 event_id=%s error=%s: %s",
                        msg.event_id, type(e).__name__, str(e).replace("\n", " ")[:500])
            status, result, code = "failed", fallback, "ANALYSIS_INVALID"
        # Immutable event input plus lease guard prevents a reclaimed worker from overwriting the new result.
        case = None
        if event["repository"] and event["spec_snapshot"]:
            case = dict(case_id=msg.event_id, app=event["app"], repository=event["repository"],
                        target_env=event["target_env"], failed_spec=event["spec_snapshot"], diagnosis=result)
        saved = await self.repo.finish_analysis(msg.event_id, job["lease_token"], status=status, result=result,
                                                error_code=code, case=case)
        log.info("배포 분석 끝 event_id=%s app=%s env=%s review_id=%s status=%s error_code=%s case=%s",
                 msg.event_id, event["app"], event["target_env"], event["review_id"], status, code,
                 ("saved" if case else "none") if saved else "lease_lost")
