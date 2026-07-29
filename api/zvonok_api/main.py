"""call-api — the product spine (BRIEF §5.4).

Owns job state, caps and audit. Does NOT own the dial: the voice agent creates
the SIP participant, because that is what makes "start the AgentSession before
dialling" possible, and because exactly one component should be able to create a
paid PSTN leg (BRIEF §9 phase-2 trap 1). The LiveKit room is therefore the
source of truth for "a call is in flight"; this table records what we asked for.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import db, dispatch, janitor, pipeline, policy, states
from .config import settings
from .models import (
    AgentEvent,
    AgentFinal,
    CallCreated,
    CallRequest,
    CallResult,
    CallStatus,
    TranscriptResponse,
    Turn,
)

logging.basicConfig(
    level=os.getenv("ZVONOK_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("zvonok.api")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_call_id() -> str:
    """ULID-ish: millisecond timestamp then randomness, Crockford base32.

    Sortable by creation time, which makes the audit log readable without a
    join, and collision-resistant enough for a service that places a few dozen
    calls a day.
    """
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    out = []
    for _ in range(26):
        value, rem = divmod(value, 32)
        out.append(_CROCKFORD[rem])
    return "c_" + "".join(reversed(out))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.require()
    await db.connect()
    task = asyncio.create_task(janitor.run_forever(), name="janitor")
    logger.info(
        "call-api up: agent=%s livekit=%s extractor=%s identities=%s",
        settings.agent_name, settings.livekit_url, settings.extractor_model,
        sorted(set(settings.tokens.values())),
    )
    try:
        yield
    finally:
        task.cancel()
        await db.close()


app = FastAPI(title="zvonok call-api", version="2.0", lifespan=lifespan)


# --- auth -------------------------------------------------------------------


def _bearer(header: str | None) -> str:
    if not header or not header.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return header[7:].strip()


async def client_identity(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Per-client identity, not just authentication.

    Every job records who asked for it, and caps are per identity (BRIEF §5.5,
    §6) — so a misbehaving OpenClaw cannot spend mac-claude's daily budget.
    """
    token = _bearer(authorization)
    for known, identity in settings.tokens.items():
        if secrets.compare_digest(token, known):
            return identity
    raise HTTPException(401, "unknown token")


async def internal_only(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """The agent worker's token. Can report call outcomes; cannot place calls."""
    if not secrets.compare_digest(_bearer(authorization), settings.internal_token):
        raise HTTPException(401, "unknown internal token")


ClientId = Annotated[str, Depends(client_identity)]


@app.exception_handler(policy.PolicyError)
async def _policy_error(_: Request, exc: policy.PolicyError) -> JSONResponse:
    # 422, not 500: the request was understood and deliberately refused. These
    # messages are written to be shown to an agent so it can correct itself.
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# --- caps -------------------------------------------------------------------


async def _enforce_caps(
    conn, identity: str, destination: policy.Destination, max_duration: int
) -> float:
    """All spend gates, before anything is dispatched (BRIEF §9 phase-2 trap 6).

    These MUST live here rather than in the agent: the agent's own concurrency
    cap is a per-worker safety net, and a second worker cannot see the first
    one's load.

    Runs inside the admission transaction (see db.admission) — every check here
    was previously check-then-act, so two simultaneous requests could both pass
    a cap that only had room for one.

    Calls already in flight are counted at their CAP, not at zero. Billing lands
    only when a call ends, so a budget that ignores in-flight work lets a client
    admit its whole day's spend in the seconds before the first call finishes.
    """
    projected = policy.estimate_usd(
        destination.country, max_duration, destination.caller_id
    )

    row = await db.spend_today(identity, conn)
    calls = int(row["calls"]) if row else 0
    seconds = float(row["seconds"]) if row else 0.0
    usd = float(row["est_usd"]) if row else 0.0

    open_jobs = await db.open_jobs(conn)
    mine = [j for j in open_jobs if j["identity"] == identity]
    reserved_seconds = sum(j["max_duration_seconds"] for j in mine)
    reserved_usd = sum(
        policy.estimate_usd(j["country"], j["max_duration_seconds"], j["caller_id"])
        for j in mine
    )

    if calls >= settings.daily_calls_per_identity:
        raise HTTPException(429, f"daily call cap reached for {identity} ({calls})")

    minutes = (seconds + reserved_seconds + max_duration) / 60.0
    if minutes > settings.daily_minutes_per_identity:
        raise HTTPException(
            429,
            f"daily minute cap would be exceeded for {identity}: "
            f"{minutes:.1f} min > {settings.daily_minutes_per_identity} "
            f"(includes {reserved_seconds / 60:.1f} min already in flight)",
        )

    if usd + reserved_usd + projected > settings.daily_usd_per_identity:
        raise HTTPException(
            429,
            f"daily spend cap would be exceeded for {identity}: "
            f"${usd:.2f} spent + ${reserved_usd:.2f} in flight + "
            f"${projected:.2f} projected > ${settings.daily_usd_per_identity:.2f}",
        )

    if len(open_jobs) >= settings.max_concurrent_calls:
        raise HTTPException(
            429,
            f"{len(open_jobs)} call(s) already in flight (cap "
            f"{settings.max_concurrent_calls}): "
            f"{', '.join(j['id'] for j in open_jobs)}",
        )

    # Not dialling twice is the actual content of idempotency (BRIEF §9 trap 2),
    # and a same-number in-flight check catches the case a key cannot: two
    # different agents, two different keys, one increasingly annoyed restaurant.
    for job in open_jobs:
        if job["number"] == destination.number:
            raise HTTPException(
                409,
                f"a call to {destination.number} is already in flight as "
                f"{job['id']} ({job['call_status']})",
            )

    return projected


# --- placing a call ---------------------------------------------------------


@app.post("/v1/calls", response_model=CallCreated, status_code=202)
async def create_call(req: CallRequest, identity: ClientId) -> CallCreated:
    destination = policy.check_destination(req.number, req.caller_id)
    max_duration = min(
        req.max_duration_seconds or settings.max_duration_default,
        settings.max_duration_hard,
    )
    disclosure_level = policy.disclosure_level_for(
        destination.country, req.goal, req.disclosure_level
    )
    job_id = new_call_id()
    room = dispatch.room_name_for(job_id)

    # Everything that decides whether this call may happen — the idempotency
    # lookup, all four caps, and the insert that consumes a slot — happens under
    # one lock. The insert is inside deliberately: the row is what makes the slot
    # visible, so the slow LiveKit dispatch can safely happen after the commit.
    async with db.admission() as conn:
        if req.idempotency_key:
            existing = await db.get_job_by_idempotency(identity, req.idempotency_key, conn)
            if existing is not None:
                # Always return the existing job, even if it ended in
                # busy/no_answer. A retry is expressed by a NEW key; reusing a
                # key must never be able to place a second call, because the
                # commonest cause of a repeated request is a client that never
                # saw our response.
                logger.info(
                    "idempotent hit for %s/%s → %s",
                    identity, req.idempotency_key, existing["id"],
                )
                return _created_from(existing, deduplicated=True)

        projected = await _enforce_caps(conn, identity, destination, max_duration)

        await db.insert_job({
            "id": job_id,
            "identity": identity,
            "idempotency_key": req.idempotency_key,
            "number": destination.number,
            "country": destination.country,
            "goal": req.goal,
            "language": req.language,
            "caller_id": destination.caller_id,
            "answer_schema": req.answer_schema,
            "disclosure_level": disclosure_level,
            "profile": req.profile,
            "max_duration_seconds": max_duration,
            "callback_url": req.callback_url,
            "call_status": "queued",
            "room_name": room,
        }, conn)
        await db.bump_spend(identity, calls=1, conn=conn)

    await db.log_event(
        "call.requested",
        job_id=job_id,
        identity=identity,
        detail={
            "number": destination.number,
            "country": destination.country,
            "caller_id": destination.caller_id,
            "goal": req.goal,
            "language": req.language,
            "disclosure_level": disclosure_level,
            "max_duration_seconds": max_duration,
            "projected_usd": projected,
        },
    )

    metadata = {
        "job_id": job_id,
        "number": destination.number,
        "goal": req.goal,
        "language": req.language,
        "caller_id": destination.caller_id,
        "max_duration_seconds": max_duration,
        "disclosure_level": disclosure_level,
        "introduce_as": req.introduce_as,
        "profile": req.profile,
        # Fed to the agent as well as to the extractor: otherwise the agent never
        # thinks to ASK for a field the schema requires (BRIEF §9 trap 8).
        "answer_schema": req.answer_schema,
    }

    # The attempt row is opened BEFORE the dispatch. An instant carrier
    # rejection can post its final report before this function gets to run
    # again, and a final that finds no attempt row silently closes nothing.
    await db.open_attempt(
        job_id, 1, room_name=room, caller_id=destination.caller_id,
        call_status="provisioning",
    )

    try:
        await db.update_job(job_id, call_status="provisioning")
        _, dispatch_id = await dispatch.dispatch_call(job_id, metadata)
    except Exception as e:  # noqa: BLE001
        logger.exception("dispatch failed for %s", job_id)
        await db.update_job(
            job_id, call_status="failed", disposition="setup_failed",
            processing_status="skipped", error=f"dispatch failed: {e}",
            ended_at=datetime.now(timezone.utc),
        )
        # No call was placed, so it must not count against the daily allowance.
        await db.bump_spend(identity, calls=-1)
        await db.log_event("call.dispatch_failed", job_id=job_id, detail={"error": str(e)[:500]})
        raise HTTPException(502, f"could not dispatch the call: {e}") from e

    await db.update_job(
        job_id, dispatch_id=dispatch_id, dispatched_at=datetime.now(timezone.utc)
    )
    await db.open_attempt(job_id, 1, dispatch_id=dispatch_id)

    job = await db.get_job(job_id)
    if req.wait_seconds:
        job = await _wait_for_early_verdict(job_id, req.wait_seconds) or job

    return _created_from(job, est=projected)


async def _wait_for_early_verdict(job_id: str, seconds: int):
    """Bounded wait that catches instant failures only.

    Deliberately NOT "wait for the call to finish" (BRIEF §9 phase-2 trap 7).
    It returns as soon as the carrier has said busy/invalid/rejected, or as soon
    as the callee picks up — the two things worth blocking a few seconds for.
    A conversation takes minutes and an agent must never block a turn on it.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(0.4)
        job = await db.get_job(job_id)
        if job is None:
            return None
        if job["call_status"] in states.TERMINAL_CALL_STATES:
            return job
        if job["call_status"] == "in_progress":
            return job
    return await db.get_job(job_id)


def _created_from(job, *, deduplicated: bool = False, est: float | None = None) -> CallCreated:
    return CallCreated(
        call_id=job["id"],
        call_status=job["call_status"],
        processing_status=job["processing_status"],
        number=job["number"],
        country=job["country"],
        caller_id=job["caller_id"],
        disclosure_level=job["disclosure_level"],
        est_cost_usd_at_cap=est,
        deduplicated=deduplicated,
        disposition=job["disposition"],
        sip_status=job["sip_status"],
    )


# --- reading a call ---------------------------------------------------------


async def _job_or_404(call_id: str, identity: str):
    job = await db.get_job(call_id)
    if job is None:
        raise HTTPException(404, f"no call {call_id}")
    # Identity scoping: one agent's call ids are not another agent's to read.
    if job["identity"] != identity:
        raise HTTPException(404, f"no call {call_id}")
    return job


@app.get("/v1/calls/{call_id}", response_model=CallStatus)
async def get_call(call_id: str, identity: ClientId) -> CallStatus:
    job = await _job_or_404(call_id, identity)
    result = await db.get_result(call_id)
    return CallStatus(
        call_id=job["id"],
        call_status=job["call_status"],
        processing_status=job["processing_status"],
        disposition=job["disposition"],
        unreachable_reason=job["unreachable_reason"],
        sip_status=job["sip_status"],
        number=job["number"],
        country=job["country"],
        caller_id=job["caller_id"],
        language=job["language"],
        goal=job["goal"],
        disclosure_level=job["disclosure_level"],
        duration_seconds=float(job["duration_seconds"]) if job["duration_seconds"] is not None else None,
        est_cost_usd=float(job["est_cost_usd"]) if job["est_cost_usd"] is not None else None,
        created_at=job["created_at"].isoformat(),
        answered_at=job["answered_at"].isoformat() if job["answered_at"] else None,
        ended_at=job["ended_at"].isoformat() if job["ended_at"] else None,
        error=job["error"],
        summary=result["summary"] if result else None,
    )


@app.get("/v1/calls/{call_id}/transcript", response_model=TranscriptResponse)
async def get_transcript(call_id: str, identity: ClientId) -> TranscriptResponse:
    job = await _job_or_404(call_id, identity)
    rows = await db.get_turns(call_id)
    return TranscriptResponse(
        call_id=call_id,
        call_status=job["call_status"],
        disposition=job["disposition"],
        # A callee who declined transcription was told out loud nothing would be
        # kept, and the agent honoured that literally (BRIEF §8). Say so, rather
        # than returning an empty list that reads like a silent call.
        redacted=job["disposition"] == "declined",
        turns=[
            Turn(
                speaker=r["speaker"], text=r["text"],
                t=float(r["t"]) if r["t"] is not None else None,
                interrupted=r["interrupted"],
                confidence=float(r["confidence"]) if r["confidence"] is not None else None,
            )
            for r in rows
        ],
    )


@app.get("/v1/calls/{call_id}/result", response_model=CallResult)
async def get_call_result(call_id: str, identity: ClientId) -> CallResult:
    job = await _job_or_404(call_id, identity)
    result = await db.get_result(call_id)
    return CallResult(
        call_id=call_id,
        call_status=job["call_status"],
        processing_status=job["processing_status"],
        disposition=job["disposition"],
        duration_seconds=float(job["duration_seconds"]) if job["duration_seconds"] is not None else None,
        answers=result["answers"] if result else None,
        summary=result["summary"] if result else None,
        goal_achieved=result["goal_achieved"] if result else None,
        captured=result["captured"] if result else None,
        unreliable_fields=result["unreliable_fields"] if result else None,
        est_cost_usd=float(job["est_cost_usd"]) if job["est_cost_usd"] is not None else None,
        extraction_error=result["error"] if result else None,
        transcript_url=f"/v1/calls/{call_id}/transcript",
    )


@app.get("/v1/calls")
async def list_calls(
    identity: ClientId,
    since: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    if since:
        try:
            cutoff = datetime.fromisoformat(since)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except ValueError as e:
            raise HTTPException(422, f"since must be ISO-8601: {e}") from e

    rows = await db.pool().fetch(
        """
        SELECT id, number, country, language, call_status, processing_status,
               disposition, duration_seconds, est_cost_usd, created_at, goal
        FROM jobs WHERE identity = $1 AND created_at >= $2
        ORDER BY created_at DESC LIMIT $3
        """,
        identity, cutoff, limit,
    )
    return [
        {
            **dict(r),
            "created_at": r["created_at"].isoformat(),
            "duration_seconds": float(r["duration_seconds"]) if r["duration_seconds"] is not None else None,
            "est_cost_usd": float(r["est_cost_usd"]) if r["est_cost_usd"] is not None else None,
        }
        for r in rows
    ]


@app.post("/v1/calls/{call_id}/cancel")
async def cancel_call(call_id: str, identity: ClientId) -> dict[str, Any]:
    job = await _job_or_404(call_id, identity)
    if job["call_status"] in states.TERMINAL_CALL_STATES:
        raise HTTPException(409, f"call {call_id} already ended ({job['call_status']})")

    room = job["room_name"] or dispatch.room_name_for(call_id)
    # Order matters: revoke the pending job FIRST, then drop any live room.
    # A dispatch that has not been claimed has no room to delete, and leaving it
    # armed would let the agent dial a call the API has already written off.
    revoked = await dispatch.revoke_dispatch(room, job["dispatch_id"])
    dropped = await dispatch.hangup(room)
    await db.update_job(
        call_id, call_status="canceled", disposition="canceled",
        processing_status="skipped", ended_at=datetime.now(timezone.utc),
    )
    await db.log_event("call.canceled", job_id=call_id, identity=identity,
                       detail={"room_existed": dropped, "dispatch_revoked": revoked})
    return {
        "call_id": call_id,
        "call_status": "canceled",
        "room_existed": dropped,
        "dispatch_revoked": revoked,
    }


@app.post("/v1/calls/{call_id}/reextract", response_model=CallResult)
async def reextract(call_id: str, identity: ClientId) -> CallResult:
    """Re-run the answer extraction over a transcript we already have.

    BRIEF §4 requires this and phase 2 shipped without it, which quietly made
    the two-status design pointless: with no way to retry a failed extraction,
    the only route back to an answer was placing the call again — spending money
    on the phone network to fix a text-model problem, which is exactly what
    separating `call_status` from `processing_status` exists to prevent.
    """
    job = await _job_or_404(call_id, identity)
    if job["call_status"] not in states.TERMINAL_CALL_STATES:
        raise HTTPException(409, f"call {call_id} has not finished yet")
    if job["disposition"] == "declined":
        raise HTTPException(
            409,
            "the callee declined transcription, so their words were discarded "
            "as promised — there is nothing to extract from (BRIEF §8)",
        )
    if not await db.get_turns(call_id):
        raise HTTPException(409, f"call {call_id} has no transcript to extract from")

    await db.update_job(call_id, processing_status="pending")
    await db.log_event("extraction.requested", job_id=call_id, identity=identity)
    await pipeline.run_extraction(await db.get_job(call_id))
    return await get_call_result(call_id, identity)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    open_jobs = await db.open_jobs()
    return {
        "ok": True,
        "in_flight": [j["id"] for j in open_jobs],
        "concurrency_cap": settings.max_concurrent_calls,
    }


# --- internal: the agent reporting in ---------------------------------------


@app.post("/v1/internal/calls/{job_id}/events", dependencies=[Depends(internal_only)])
async def agent_event(job_id: str, event: AgentEvent) -> dict[str, str]:
    """Advisory progress. Never moves a job backwards (see db.advance_call_status)."""
    job = await db.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"no call {job_id}")

    mapping = {
        "dispatched": "provisioning",
        "dialing": "dialing",
        "ringing": "ringing",
        "answered": "in_progress",
        "ending": "ending",
    }
    await db.advance_call_status(job_id, mapping[event.event])
    if event.event == "answered" and job["answered_at"] is None:
        await db.update_job(job_id, answered_at=datetime.now(timezone.utc))
    await db.log_event(f"call.{event.event}", job_id=job_id, detail=event.detail or {})
    return {"ok": "true"}


@app.post("/v1/internal/calls/{job_id}/final", dependencies=[Depends(internal_only)])
async def agent_final(job_id: str, final: AgentFinal) -> dict[str, str]:
    """The end-of-call report. Idempotent by design: delivery is at-least-once,
    so a redelivery after a timed-out POST is expected traffic (BRIEF §9 trap 4)."""
    job = await db.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"no call {job_id}")
    await pipeline.record_final(job, final)
    return {"ok": "true", "call_id": job_id}
