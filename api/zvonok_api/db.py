"""Postgres access. asyncpg + plain SQL — no ORM for six tables."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from .config import settings

logger = logging.getLogger("zvonok.db")

# Arbitrary constant; the only requirement is that every call-api process agrees.
_ADMISSION_LOCK = 0x7A766F6E  # "zvon"

_pool: asyncpg.Pool | None = None

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=1,
            max_size=8,
            # asyncpg returns JSONB as str unless told otherwise, and every
            # caller here wants the decoded value.
            init=_register_json,
        )
        async with _pool.acquire() as conn:
            await conn.execute(SCHEMA_PATH.read_text())
        logger.info("database ready")
    return _pool


async def _register_json(conn: asyncpg.Connection) -> None:
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db.connect() has not run")
    return _pool


@asynccontextmanager
async def admission():
    """Serialise the whole admit-a-call decision.

    Every spend gate in BRIEF §6 was check-then-act: two simultaneous POSTs both
    read zero open jobs and an unspent budget, both passed, and both dialled —
    blowing the concurrency cap and calling the same person twice. A cap that a
    second concurrent request can walk through is not a cap.

    A single advisory lock held for the transaction fixes it. Contention is
    irrelevant here (this service places a few dozen calls a day) and the
    alternative — SELECT ... FOR UPDATE across three tables in the right order —
    is more code and more ways to deadlock.

    The insert must happen INSIDE this block: the job row is what makes the slot
    visible to the next request, so the row itself becomes the mutex and the
    slow LiveKit dispatch can safely happen after the lock is released.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _ADMISSION_LOCK)
            yield conn


# --- audit ------------------------------------------------------------------


async def log_event(
    kind: str,
    *,
    job_id: str | None = None,
    identity: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append-only audit (BRIEF §6). Never raises: losing an audit line must not
    fail the operation being audited, but it must be visible in the log."""
    try:
        await pool().execute(
            "INSERT INTO events (job_id, identity, kind, detail) VALUES ($1,$2,$3,$4)",
            job_id, identity, kind, detail or {},
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not write audit event %s for job %s", kind, job_id)


# --- jobs -------------------------------------------------------------------


async def get_job(job_id: str) -> asyncpg.Record | None:
    return await pool().fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)


async def get_job_by_idempotency(
    identity: str, key: str, conn: asyncpg.Connection | None = None
) -> asyncpg.Record | None:
    return await (conn or pool()).fetchrow(
        "SELECT * FROM jobs WHERE identity = $1 AND idempotency_key = $2",
        identity, key,
    )


async def open_jobs(conn: asyncpg.Connection | None = None) -> list[asyncpg.Record]:
    """Jobs that have not reached a terminal call state.

    This is the concurrency counter AND the "is something already in flight to
    this number" check, so it must not be approximated by a time window.
    """
    return await (conn or pool()).fetch(
        """
        SELECT * FROM jobs
        WHERE call_status NOT IN (
            'completed','busy','no_answer','rejected','voicemail',
            'failed','canceled','timed_out','invalid_number')
        ORDER BY created_at
        """
    )


async def insert_job(job: dict[str, Any], conn: asyncpg.Connection | None = None) -> None:
    columns = list(job)
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    await (conn or pool()).execute(
        f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders})",
        *job.values(),
    )


async def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.now(timezone.utc)
    assignments = ", ".join(f"{k} = ${i}" for i, k in enumerate(fields, start=2))
    await pool().execute(
        f"UPDATE jobs SET {assignments} WHERE id = $1", job_id, *fields.values()
    )


async def advance_call_status(job_id: str, status: str) -> None:
    """Move a job forward, never backward.

    Progress pings from the agent are advisory and arrive out of order over the
    network. Guarding only against terminal states was not enough: an `answered`
    ping that commits `in_progress` followed by a delayed `dialing` ping would
    walk the job backwards, and `wait_seconds` reads that state to decide
    whether the callee has picked up. So compare lifecycle RANK, not just
    terminality.
    """
    from .states import CALL_STATES, TERMINAL_CALL_STATES

    rank = CALL_STATES.index(status) if status in CALL_STATES else 0
    ranked = list(CALL_STATES[: rank + 1])

    await pool().execute(
        """
        UPDATE jobs SET call_status = $2, updated_at = now()
        WHERE id = $1
          AND call_status <> ALL($3::text[])   -- never leave a terminal state
          AND call_status = ANY($4::text[])    -- never move backwards
        """,
        job_id, status, list(TERMINAL_CALL_STATES), ranked,
    )


# --- turns / results --------------------------------------------------------


async def replace_turns(job_id: str, attempt_no: int, turns: list[dict[str, Any]]) -> None:
    """Idempotent by (job_id, attempt_no): a redelivered final report overwrites
    rather than duplicating."""
    async with pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM turns WHERE job_id = $1 AND attempt_no = $2", job_id, attempt_no
        )
        if not turns:
            return
        await conn.executemany(
            """
            INSERT INTO turns (job_id, attempt_no, idx, speaker, text, t, interrupted, confidence)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            [
                (
                    job_id, attempt_no, i, t.get("speaker", "unknown"),
                    t.get("text", ""), t.get("t"),
                    bool(t.get("interrupted", False)), t.get("confidence"),
                )
                for i, t in enumerate(turns)
            ],
        )


async def get_turns(job_id: str) -> list[asyncpg.Record]:
    return await pool().fetch(
        "SELECT * FROM turns WHERE job_id = $1 ORDER BY attempt_no, idx", job_id
    )


async def upsert_result(job_id: str, **fields: Any) -> None:
    fields.setdefault("captured", None)
    columns = ["job_id", *fields]
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields)
    await pool().execute(
        f"""
        INSERT INTO results ({', '.join(columns)}) VALUES ({placeholders})
        ON CONFLICT (job_id) DO UPDATE SET {updates}, created_at = now()
        """,
        job_id, *fields.values(),
    )


async def get_result(job_id: str) -> asyncpg.Record | None:
    return await pool().fetchrow("SELECT * FROM results WHERE job_id = $1", job_id)


# --- attempts ---------------------------------------------------------------


async def open_attempt(job_id: str, attempt_no: int, **fields: Any) -> None:
    columns = ["job_id", "attempt_no", *fields]
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields) or "attempt_no = EXCLUDED.attempt_no"
    await pool().execute(
        f"""
        INSERT INTO attempts ({', '.join(columns)}) VALUES ({placeholders})
        ON CONFLICT (job_id, attempt_no) DO UPDATE SET {updates}
        """,
        job_id, attempt_no, *fields.values(),
    )


async def close_attempt(job_id: str, attempt_no: int, **fields: Any) -> None:
    fields["ended_at"] = datetime.now(timezone.utc)
    assignments = ", ".join(f"{k} = ${i}" for i, k in enumerate(fields, start=3))
    await pool().execute(
        f"UPDATE attempts SET {assignments} WHERE job_id = $1 AND attempt_no = $2",
        job_id, attempt_no, *fields.values(),
    )


async def next_attempt_no(job_id: str) -> int:
    row = await pool().fetchval(
        "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM attempts WHERE job_id = $1", job_id
    )
    return int(row or 1)


async def current_attempt_no(job_id: str) -> int:
    """The attempt a final report belongs to.

    Defaults to 1 rather than 0: an instant carrier rejection can post its final
    before the API has finished writing the attempt row, and the report still
    describes attempt 1.
    """
    row = await pool().fetchval(
        "SELECT COALESCE(MAX(attempt_no), 1) FROM attempts WHERE job_id = $1", job_id
    )
    return int(row or 1)


# --- finalisation ------------------------------------------------------------


async def claim_finalisation(job_id: str) -> bool:
    """Atomically decide whether THIS report gets to bill and set the outcome.

    Two independent things deliver the same end-of-call report — the agent's
    HTTP POST and the janitor recovering the file from disk — and they can run
    concurrently. Reading `call_status` beforehand and branching on it is a race:
    both readers see a non-terminal job, both add the duration to the daily spend
    counter, and the identity is billed twice for one call.

    It is also the guard that stops a late agent report from resurrecting a job
    an operator explicitly CANCELLED, or one the janitor already wrote off.
    Whoever gets here first owns the outcome; later arrivals may still refresh
    the transcript, but not the money or the verdict.
    """
    # `post_processing` is the claim marker specifically because no progress
    # event maps to it — the agent's own "ending" ping ranks below it, so a
    # late ping cannot claim finalisation out from under the real report.
    row = await pool().fetchrow(
        """
        UPDATE jobs SET call_status = 'post_processing', updated_at = now()
        WHERE id = $1 AND call_status <> ALL($2::text[])
        RETURNING id
        """,
        job_id,
        [
            "completed", "busy", "no_answer", "rejected", "voicemail",
            "failed", "canceled", "timed_out", "invalid_number",
            "post_processing",
        ],
    )
    return row is not None


# --- spend ------------------------------------------------------------------


async def spend_today(
    identity: str, conn: asyncpg.Connection | None = None
) -> asyncpg.Record | None:
    return await (conn or pool()).fetchrow(
        "SELECT * FROM spend WHERE day = $1 AND identity = $2",
        date.today(), identity,
    )


async def bump_spend(
    identity: str,
    *,
    calls: int = 0,
    seconds: float = 0.0,
    usd: float = 0.0,
    conn: asyncpg.Connection | None = None,
) -> None:
    await (conn or pool()).execute(
        """
        INSERT INTO spend (day, identity, calls, seconds, est_usd)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (day, identity) DO UPDATE SET
            calls   = spend.calls   + EXCLUDED.calls,
            seconds = spend.seconds + EXCLUDED.seconds,
            est_usd = spend.est_usd + EXCLUDED.est_usd
        """,
        date.today(), identity, calls, seconds, usd,
    )


# --- webhook deliveries -----------------------------------------------------


async def queue_delivery(job_id: str, url: str, payload: dict[str, Any]) -> None:
    await pool().execute(
        """
        INSERT INTO deliveries (job_id, url, payload) VALUES ($1,$2,$3)
        ON CONFLICT (job_id) DO UPDATE SET
            payload = EXCLUDED.payload, attempts = 0,
            next_try_at = now(), delivered_at = NULL
        """,
        job_id, url, payload,
    )


async def due_deliveries(limit: int = 5) -> list[asyncpg.Record]:
    return await pool().fetch(
        """
        SELECT * FROM deliveries
        WHERE delivered_at IS NULL AND next_try_at <= now() AND attempts < 8
        ORDER BY next_try_at LIMIT $1
        """,
        limit,
    )
