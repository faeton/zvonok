-- zvonok call-api schema (BRIEF §5.4).
--
-- Applied idempotently at startup. There is no migration tool: this is a
-- single-writer personal service, and every statement here is CREATE ... IF NOT
-- EXISTS so a restart is a no-op. When a column has to change, add an explicit
-- ALTER below the table it belongs to, guarded the same way.

-- One row per job = one thing an agent asked us to accomplish by phone.
-- A job may involve several dial attempts (busy/no-answer retries, phase 3);
-- attempts are appended, never overwritten (BRIEF §4).
CREATE TABLE IF NOT EXISTS jobs (
    id                    TEXT PRIMARY KEY,
    identity              TEXT        NOT NULL,   -- bearer-token owner: mac-claude / openclaw / manual
    idempotency_key       TEXT,
    number                TEXT        NOT NULL,   -- E.164, normalised
    country               TEXT,                   -- ISO-3166-1 alpha-2, derived from prefix
    goal                  TEXT        NOT NULL,
    language              TEXT        NOT NULL,
    caller_id             TEXT,
    answer_schema         JSONB,
    disclosure_level      TEXT        NOT NULL DEFAULT 'light',
    profile               TEXT        NOT NULL DEFAULT 'grok-voice',
    max_duration_seconds  INTEGER     NOT NULL,
    callback_url          TEXT,

    -- Two independent statuses, deliberately (BRIEF §4). A call that completed
    -- fine but whose extraction failed is NOT a failed call and must never be
    -- redialled — that would spend money to fix a text-model problem.
    call_status           TEXT        NOT NULL DEFAULT 'queued',
    processing_status     TEXT        NOT NULL DEFAULT 'pending',

    disposition           TEXT,
    unreachable_reason    TEXT,
    sip_status            TEXT,
    room_name             TEXT,
    dispatch_id           TEXT,
    duration_seconds      NUMERIC,
    est_cost_usd          NUMERIC,
    error                 TEXT,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at         TIMESTAMPTZ,
    answered_at           TIMESTAMPTZ,
    ended_at              TIMESTAMPTZ
);

-- Idempotency is scoped per identity: two different agents using the same
-- boring key ("call-hotel") must not collide, but one agent retrying its own
-- request must not dial twice (BRIEF §9 phase-2 trap 2).
-- Which billing account this call was ADMITTED under. Stored, not derived from
-- `identity` at read time, and the difference is money: the tenant decides
-- whose xAI key reads the transcript and whose numbers the call went out on.
-- Re-deriving it later means editing ZVONOK_TENANT_<IDENTITY> — or removing the
-- mapping while jobs are unextracted — silently moves extraction, /reextract
-- and the janitor's disk recovery onto the new mapping, handing one tenant's
-- transcript to another tenant's key. Dispatch already fixes the trunk at
-- placement; this makes the accounting equally immutable.
--
-- Nullable, because rows written before the column existed cannot be backfilled
-- from SQL: the identity→tenant mapping lives in env, not in the database.
-- Settings.tenant_of() falls back to the derived answer for those.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tenant TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS jobs_idem_key
    ON jobs (identity, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS jobs_created_at ON jobs (created_at DESC);

-- "Who is this, and what did we call them about?" — the question a callback
-- asks, and the one shape this table could not answer. Every call was recorded
-- in full and none of it was reachable by the only key an incoming call gives
-- you: the number. Not a new store, just the missing direction on the old one.
--
-- `identity` leads because the query filters on both and reads are always
-- identity-scoped. Keyed on number alone, a shared business number that several
-- identities had called would make Postgres walk everyone else's rows to find
-- the ten belonging to the caller.
CREATE INDEX IF NOT EXISTS jobs_identity_number
    ON jobs (identity, number, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_open ON jobs (call_status)
    WHERE call_status NOT IN (
        'completed', 'busy', 'no_answer', 'rejected', 'voicemail',
        'failed', 'canceled', 'timed_out', 'invalid_number'
    );

CREATE TABLE IF NOT EXISTS attempts (
    id               BIGSERIAL PRIMARY KEY,
    job_id           TEXT      NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    attempt_no       INTEGER   NOT NULL,
    room_name        TEXT,
    dispatch_id      TEXT,
    caller_id        TEXT,
    call_status      TEXT,
    disposition      TEXT,
    sip_status       TEXT,
    duration_seconds NUMERIC,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at         TIMESTAMPTZ,
    UNIQUE (job_id, attempt_no)
);

-- `interrupted` and `confidence` are load-bearing, not decoration (BRIEF §9
-- phase-2 trap 9): barge-in truncation means a stored assistant turn can contain
-- words the human never actually heard, and an extractor that treats those as
-- spoken will "confirm" something that was never said aloud.
CREATE TABLE IF NOT EXISTS turns (
    job_id      TEXT    NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    attempt_no  INTEGER NOT NULL DEFAULT 1,
    idx         INTEGER NOT NULL,
    speaker     TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    t           NUMERIC,
    interrupted BOOLEAN NOT NULL DEFAULT false,
    confidence  NUMERIC,
    PRIMARY KEY (job_id, attempt_no, idx)
);

CREATE TABLE IF NOT EXISTS results (
    job_id            TEXT PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    answers           JSONB,
    summary           TEXT,
    goal_achieved     BOOLEAN,
    -- What the agent read back to the callee and got agreement on, via the
    -- record_answer tool. Kept separate from `answers` so the two can disagree —
    -- disagreement is the signal that a value is unreliable (BRIEF §9 trap 8).
    captured          JSONB,
    unreliable_fields JSONB,
    extractor_model   TEXT,
    prompt_hash       TEXT,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only audit (BRIEF §6). Keeps who-asked-what with every job.
CREATE TABLE IF NOT EXISTS events (
    id       BIGSERIAL PRIMARY KEY,
    job_id   TEXT,
    identity TEXT,
    kind     TEXT NOT NULL,
    detail   JSONB,
    at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS events_job ON events (job_id, at);

-- Daily counters per identity, for the caps in BRIEF §6. Cost here is an
-- ESTIMATE from the phase-0 price table; reconciliation against Zadarma's
-- /v1/statistics/ is phase 3 (and has two traps documented in BRIEF §7.1).
CREATE TABLE IF NOT EXISTS spend (
    day      DATE    NOT NULL,
    identity TEXT    NOT NULL,
    calls    INTEGER NOT NULL DEFAULT 0,
    seconds  NUMERIC NOT NULL DEFAULT 0,
    est_usd  NUMERIC NOT NULL DEFAULT 0,
    PRIMARY KEY (day, identity)
);

-- Webhook delivery is at-least-once with backoff; state lives here so a
-- call-api restart does not drop a pending callback.
CREATE TABLE IF NOT EXISTS deliveries (
    id           BIGSERIAL PRIMARY KEY,
    job_id       TEXT        NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    url          TEXT        NOT NULL,
    payload      JSONB       NOT NULL,
    attempts     INTEGER     NOT NULL DEFAULT 0,
    next_try_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ,
    last_error   TEXT,
    UNIQUE (job_id)
);
