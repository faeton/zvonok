"""Reporting the call back to call-api.

Delivery is **at-least-once, keyed by job_id** (BRIEF §9 phase-2 trap 4). Two
properties matter and neither is negotiable:

- A lost POST must never look like a call that never happened. So the transcript
  is written to disk *before* it is posted, and call-api's janitor picks the file
  up from the shared volume if the HTTP path fails entirely.
- Reporting must never be able to keep a paid line open. Every call here is
  bounded in total time; failing to report is bad, hanging up late costs money.

If ZVONOK_API_URL is unset the agent runs exactly as it did in phase 1 — dispatch
by hand with lkctl.sh, transcript to a file — which keeps the manual path usable
for debugging without call-api in the loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("zvonok.report")

API_URL = os.getenv("ZVONOK_API_URL", "").rstrip("/")
INTERNAL_TOKEN = os.getenv("ZVONOK_INTERNAL_TOKEN", "")

_ENABLED = bool(API_URL and INTERNAL_TOKEN)
if not _ENABLED:
    logger.info("call-api reporting disabled (ZVONOK_API_URL/ZVONOK_INTERNAL_TOKEN unset)")


def enabled() -> bool:
    return _ENABLED


async def post_event(job_id: str, event: str, **detail: Any) -> None:
    """Advisory progress ping. One try, short timeout, failures swallowed.

    These only exist so a client polling mid-call sees `ringing` rather than
    `provisioning`; losing one changes nothing about the outcome, so it is not
    worth a retry loop that could delay the call.
    """
    if not _ENABLED:
        return
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            await client.post(
                f"{API_URL}/v1/internal/calls/{job_id}/events",
                json={"event": event, "detail": detail or None},
                headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
            )
    except Exception as e:  # noqa: BLE001 — progress pings are best-effort
        logger.info("progress ping %s for %s failed: %r", event, job_id, e)


async def post_final(body: dict[str, Any], *, budget_seconds: float = 30.0) -> bool:
    """The end-of-call report. Retries within a hard time budget.

    Runs from the shutdown callback, after the transcript is already on disk, so
    the worst case is a delayed reconciliation rather than a lost call.
    """
    if not _ENABLED:
        return False

    job_id = body.get("job_id")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    backoff = 1.0

    async with httpx.AsyncClient(timeout=10) as client:
        attempt = 0
        while loop.time() < deadline:
            attempt += 1
            try:
                resp = await client.post(
                    f"{API_URL}/v1/internal/calls/{job_id}/final",
                    json=body,
                    headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
                )
                if resp.status_code < 300:
                    logger.info("final report delivered for %s", job_id)
                    return True
                # 404 means call-api has no such job — retrying cannot fix that,
                # and the file on disk is the right record either way.
                if resp.status_code == 404:
                    logger.error("call-api does not know job %s — not retrying", job_id)
                    return False
                logger.warning(
                    "final report for %s rejected: HTTP %s %s",
                    job_id, resp.status_code, resp.text[:200],
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("final report attempt %d for %s failed: %r", attempt, job_id, e)

            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(backoff, remaining))
            backoff *= 2

    logger.error(
        "could not deliver final report for %s within %.0fs — call-api will "
        "recover it from the transcript file",
        job_id, budget_seconds,
    )
    return False
