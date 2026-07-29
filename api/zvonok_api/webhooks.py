"""Optional HMAC-signed callbacks (BRIEF §5.4).

Polling is always available and is the primary interface; a webhook is a
latency optimisation. So delivery failures degrade the experience but never the
correctness of a job, and nothing here is allowed to affect call state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import httpx

from . import db
from .config import settings

logger = logging.getLogger("zvonok.webhooks")


def sign(body: bytes, timestamp: str) -> str:
    """`t.body` HMAC, so a captured signature cannot be replayed with a new body
    or an old body with a new timestamp."""
    mac = hmac.new(
        settings.webhook_secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    )
    return mac.hexdigest()


async def deliver_due() -> None:
    rows = await db.due_deliveries()
    if not rows:
        return

    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for row in rows:
            body = json.dumps(row["payload"], ensure_ascii=False).encode()
            ts = str(int(time.time()))
            headers = {
                "content-type": "application/json",
                "x-zvonok-timestamp": ts,
                "x-zvonok-event": "call.completed",
                # Consumers dedupe on this: delivery is at-least-once.
                "x-zvonok-delivery-id": f"{row['job_id']}:call.completed",
            }
            if settings.webhook_secret:
                headers["x-zvonok-signature"] = f"sha256={sign(body, ts)}"

            attempts = row["attempts"] + 1
            try:
                resp = await client.post(row["url"], content=body, headers=headers)
                if 200 <= resp.status_code < 300:
                    await db.pool().execute(
                        "UPDATE deliveries SET delivered_at = now(), attempts = $2 WHERE id = $1",
                        row["id"], attempts,
                    )
                    continue
                error = f"HTTP {resp.status_code}"
            except httpx.HTTPError as e:
                error = repr(e)

            # 1 min, 2, 4, … capped — eight attempts spans about four hours.
            backoff = min(60 * 2 ** (attempts - 1), 3600)
            logger.warning(
                "webhook for %s failed (%s), retry %d in %ds",
                row["job_id"], error, attempts, backoff,
            )
            await db.pool().execute(
                """
                UPDATE deliveries
                SET attempts = $2, last_error = $3,
                    next_try_at = now() + make_interval(secs => $4)
                WHERE id = $1
                """,
                row["id"], attempts, error[:500], backoff,
            )
