"""Background reconciliation.

Three jobs, one loop:

1. **Reconcile open jobs.** The agent's callback can be lost — the process can
   die between hanging up and posting. A job stuck at `in_progress` forever is
   worse than a job marked failed, because it holds a concurrency slot and hides
   from every cap. So: past the grace period, consult the LiveKit room (the
   source of truth for "a call is in flight"), then the transcript on disk, then
   give up and mark it failed.
2. **Run pending extractions.** Separate from the call path on purpose (BRIEF §4).
3. **Pump webhook deliveries.**

Every pass is wrapped so one failing job cannot stop the loop for the others —
this process must survive a bad row, since it is what unsticks bad rows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, dispatch, pipeline, states, webhooks
from .config import settings

logger = logging.getLogger("zvonok.janitor")

TICK_SECONDS = 5.0


async def run_forever() -> None:
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must outlive any single failure
            logger.exception("janitor tick failed")
        await asyncio.sleep(TICK_SECONDS)


async def _tick() -> None:
    await _reconcile_open_jobs()
    await _run_pending_extractions()
    await webhooks.deliver_due()


async def _reconcile_open_jobs() -> None:
    jobs = await db.open_jobs()
    if not jobs:
        return

    now = datetime.now(timezone.utc)
    rooms: dict[str, int] | None = None

    for job in jobs:
        started = job["dispatched_at"] or job["created_at"]

        # A job that never got past `provisioning` was never picked up by a
        # worker, so there is no call to wait out. Waiting the full
        # max_duration + grace for it (~6.5 minutes) is how a single dead or
        # unregistered agent starves the concurrency cap and blocks every other
        # call on the box — the first thing that actually breaks in production.
        # Fail it fast instead; nothing was dialled and nothing was billed.
        if job["call_status"] in ("queued", "provisioning"):
            if now < started + timedelta(seconds=settings.provisioning_timeout_seconds):
                continue
            logger.error(
                "job %s never left %s after %ds — no worker picked it up",
                job["id"], job["call_status"], settings.provisioning_timeout_seconds,
            )
            await db.update_job(
                job["id"],
                call_status="failed", disposition="setup_failed",
                processing_status="skipped", ended_at=now,
                error="no agent worker picked up the dispatch",
            )
            await db.log_event(
                "call.never_dispatched", job_id=job["id"], identity=job["identity"]
            )
            # The call never happened, so it must not count against the day.
            await db.bump_spend(job["identity"], calls=-1)
            continue

        # Deadline = when the call could not possibly still be running, even if
        # it ran to its hard cap, plus grace for the callback to arrive.
        deadline = started + timedelta(
            seconds=job["max_duration_seconds"] + settings.reconcile_grace_seconds
        )
        if now < deadline:
            continue

        if rooms is None:
            try:
                rooms = await dispatch.live_rooms()
            except Exception as e:  # noqa: BLE001
                logger.warning("could not list LiveKit rooms: %r", e)
                return

        room = job["room_name"] or dispatch.room_name_for(job["id"])
        if room in rooms:
            # Still genuinely in flight past its own cap. The agent's duration
            # guard and the server-side max_call_duration should both have fired
            # by now, so this is an anomaly worth killing rather than waiting on:
            # the meter is running.
            logger.error("job %s still live past its cap — force hangup", job["id"])
            await db.log_event("call.force_hangup", job_id=job["id"], detail={"room": room})
            try:
                await dispatch.hangup(room)
            except Exception as e:  # noqa: BLE001
                logger.warning("force hangup of %s failed: %r", room, e)
            continue

        if await _recover_from_disk(job):
            continue

        logger.error(
            "job %s: no room, no transcript, no callback — marking failed", job["id"]
        )
        await db.update_job(
            job["id"],
            call_status="failed",
            disposition="failed",
            processing_status="skipped",
            ended_at=now,
            error="agent never reported and left no transcript",
        )
        await db.log_event("call.lost", job_id=job["id"], identity=job["identity"])


async def _recover_from_disk(job) -> bool:
    """The fallback half of at-least-once delivery.

    call-api and the agent both run on de1, so the agent's transcript directory
    is mounted here read-only. A callback that never arrived over HTTP is still
    recoverable from the file the agent wrote before (and independently of)
    posting it.
    """
    directory = Path(settings.transcript_dir)
    if not directory.is_dir():
        return False

    matches = sorted(directory.glob(f"*-{job['id']}.json"))
    if not matches:
        return False

    payload = pipeline.load_disk_transcript(matches[-1])
    if payload is None:
        return False

    logger.warning(
        "job %s: recovered end-of-call report from disk (%s)", job["id"], matches[-1].name
    )
    await db.log_event(
        "call.recovered_from_disk", job_id=job["id"], detail={"file": matches[-1].name}
    )
    await pipeline.record_final(job, pipeline.final_from_disk(payload))
    return True


async def _run_pending_extractions() -> None:
    rows = await db.pool().fetch(
        """
        SELECT * FROM jobs
        WHERE processing_status = 'pending'
          AND call_status = ANY($1::text[])
        ORDER BY updated_at LIMIT 3
        """,
        sorted(states.TERMINAL_CALL_STATES),
    )
    for job in rows:
        try:
            await pipeline.run_extraction(job)
        except Exception:  # noqa: BLE001
            logger.exception("extraction crashed for %s", job["id"])
            await db.update_job(job["id"], processing_status="failed")
