"""What happens to a job after the phone call ends.

Two entry points, because the agent's end-of-call report reaches us two ways —
over HTTP, and (if that fails) as a file on de1's disk. Both funnel through
`record_final`, which is idempotent: at-least-once delivery means a redelivered
report is normal traffic, not an error (BRIEF §9 phase-2 trap 4).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from . import db, extractor, policy, states
from .config import settings
from .models import AgentFinal

logger = logging.getLogger("zvonok.pipeline")


async def record_final(job: Any, final: AgentFinal) -> None:
    """Persist an end-of-call report. Safe to call more than once per job.

    The report arrives by two independent routes — the agent's HTTP POST and the
    janitor recovering the file from disk — which can overlap. So the decision
    to bill and to set the outcome is taken with an atomic claim, not by reading
    `job["call_status"]` (a snapshot that is already stale by the time we act on
    it). A later arrival still refreshes the transcript, which is harmless and
    occasionally useful, but never the money or the verdict.
    """
    job_id = job["id"]
    disposition, call_status = states.normalise(
        final.disposition, final.unreachable_reason
    )
    owns_outcome = await db.claim_finalisation(job_id)

    # Trust the agent's own normalisation only as a dialect: if it computed a
    # call_status we do not recognise, ours wins (BRIEF §9 phase-2 trap 3).
    if final.call_status in states.ALL_CALL_STATES and final.call_status != call_status:
        logger.info(
            "job %s: agent said call_status=%s, canonical mapping says %s — using %s",
            job_id, final.call_status, call_status, call_status,
        )

    # Duration for billing purposes runs from ANSWER, not from when the worker
    # started: the agent's clock begins before the dial, so ring and setup time
    # was inflating every cost estimate. The carrier bills from answer too.
    duration = float(final.duration_seconds or 0.0)
    billable = duration
    if job["answered_at"] and job["dispatched_at"]:
        ring = (job["answered_at"] - job["dispatched_at"]).total_seconds()
        billable = max(duration - max(ring, 0.0), 0.0)
    # Priced on the caller ID actually used: origin is a ×20–34 cost lever on
    # EU/UK routes (BRIEF §9 phase-0), so pricing by destination alone would
    # under-report by an order of magnitude exactly when a client overrode it.
    est = policy.estimate_usd(
        job["country"], billable, job["caller_id"],
        settings.tenant_of(job).caller_ids,
    )

    # A callee who objected to being transcribed was told out loud that nothing
    # would be kept. The agent already discarded the turns; make sure nothing
    # here writes them back, and skip extraction entirely (BRIEF §8).
    redacted = final.redacted or disposition == "declined"
    turns = [] if redacted else [t.model_dump() for t in final.turns]
    captured = [] if redacted else final.captured

    # Phase 2 places exactly one dial per job (the retry policy is phase 3), but
    # everything downstream is already keyed by attempt so retries stay additive
    # and never overwrite a previous attempt's turns (BRIEF §4).
    attempt_no = await db.current_attempt_no(job_id)
    # Refreshing the transcript on a redelivery is safe and sometimes better
    # (the second copy may be the complete one), so this happens either way.
    await db.replace_turns(job_id, attempt_no, turns)
    if captured:
        await db.upsert_result(job_id, captured=captured)

    if not owns_outcome:
        current = await db.get_job(job_id)
        logger.info(
            "job %s: end-of-call report redelivered after the outcome was "
            "already settled as %s — transcript refreshed, verdict and billing "
            "left alone",
            job_id, current["call_status"] if current else "unknown",
        )
        return

    extract = states.extraction_is_worthwhile(call_status, disposition) and bool(turns)
    await db.update_job(
        job_id,
        call_status=call_status,
        disposition=disposition,
        unreachable_reason=final.unreachable_reason,
        sip_status=final.sip_status or job["sip_status"],
        duration_seconds=duration,
        est_cost_usd=est,
        ended_at=job["ended_at"] or datetime.now(timezone.utc),
        processing_status="pending" if extract else "skipped",
    )
    await db.close_attempt(
        job_id, attempt_no,
        call_status=call_status, disposition=disposition,
        sip_status=final.sip_status, duration_seconds=duration,
    )

    await db.bump_spend(job["identity"], seconds=billable, usd=est)

    await db.log_event(
        "call.finished",
        job_id=job_id,
        identity=job["identity"],
        detail={
            "disposition": disposition,
            "call_status": call_status,
            "duration_seconds": duration,
            "billable_seconds": billable,
            "caller_id": job["caller_id"],
            "est_usd": est,
            "turns": len(turns),
            "redacted": redacted,
        },
    )
    logger.info(
        "job %s finished: %s/%s, %.1fs (%.1fs billable), ~$%.3f, %d turns%s",
        job_id, call_status, disposition, duration, billable, est, len(turns),
        " (redacted)" if redacted else "",
    )

    if not extract:
        await _queue_webhook(job_id)


async def run_extraction(job: Any) -> None:
    """The text-model pass. Never touches call state (BRIEF §4).

    A failure here marks processing_status=failed and stops. It must not mark
    the CALL as failed, because that would make a redial look like the right fix
    — spending money on the phone network to repair a text-model problem.
    """
    job_id = job["id"]
    await db.update_job(job_id, processing_status="extracting")

    rows = await db.get_turns(job_id)
    turns = [
        {
            "speaker": r["speaker"], "text": r["text"], "t": float(r["t"]) if r["t"] is not None else None,
            "interrupted": r["interrupted"],
            "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
        }
        for r in rows
    ]
    existing = await db.get_result(job_id)
    captured = (existing["captured"] if existing else None) or []

    try:
        out = await extractor.extract(
            goal=job["goal"],
            language=job["language"],
            turns=turns,
            captured=captured,
            answer_schema=job["answer_schema"],
            tenant=settings.tenant_of(job),
        )
    except Exception as e:  # noqa: BLE001 — every failure mode ends the same way
        logger.exception("extraction failed for %s", job_id)
        await db.upsert_result(job_id, error=str(e)[:1000])
        await db.update_job(job_id, processing_status="failed")
        await db.log_event("extraction.failed", job_id=job_id, detail={"error": str(e)[:500]})
        await _queue_webhook(job_id)
        return

    await db.upsert_result(
        job_id,
        answers=out.get("answers") or {},
        summary=out.get("summary"),
        goal_achieved=out.get("goal_achieved"),
        unreliable_fields=out.get("unreliable_fields") or [],
        extractor_model=out.get("model"),
        prompt_hash=out.get("prompt_hash"),
        error=None,
    )
    await db.update_job(job_id, processing_status="completed")
    await db.log_event(
        "extraction.completed",
        job_id=job_id,
        detail={
            "goal_achieved": out.get("goal_achieved"),
            "unreliable_fields": out.get("unreliable_fields"),
        },
    )
    logger.info(
        "job %s extracted: goal_achieved=%s, unreliable=%s",
        job_id, out.get("goal_achieved"), out.get("unreliable_fields"),
    )
    await _queue_webhook(job_id)


async def _queue_webhook(job_id: str) -> None:
    job = await db.get_job(job_id)
    if job is None or not job["callback_url"]:
        return
    result = await db.get_result(job_id)
    await db.queue_delivery(
        job_id,
        job["callback_url"],
        {
            "event": "call.completed",
            "call_id": job_id,
            "call_status": job["call_status"],
            "processing_status": job["processing_status"],
            "disposition": job["disposition"],
            "duration_seconds": float(job["duration_seconds"] or 0),
            "answers": (result["answers"] if result else None),
            "summary": (result["summary"] if result else None),
            "est_cost_usd": float(job["est_cost_usd"] or 0),
        },
    )


def final_from_disk(payload: dict[str, Any]) -> AgentFinal:
    """Parse a transcript file written by the agent.

    Same shape as the HTTP body by construction — the agent builds one dict and
    both writes it and posts it — but parsed leniently, because this path exists
    precisely for the case where something has already gone wrong.
    """
    return AgentFinal(
        job_id=payload.get("job_id", ""),
        call_status=payload.get("call_status"),
        disposition=payload.get("disposition"),
        unreachable_reason=payload.get("unreachable_reason"),
        sip_status=payload.get("sip_status"),
        duration_seconds=payload.get("duration_seconds"),
        redacted=bool(payload.get("redacted")),
        turns=payload.get("turns") or [],
        captured=payload.get("captured") or [],
    )


def load_disk_transcript(path: Any) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("could not read transcript %s: %r", path, e)
        return None
