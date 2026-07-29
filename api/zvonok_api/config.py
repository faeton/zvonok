"""Configuration, all from env (BRIEF §6: secrets live in env files on de1 only).

Nothing here reads a file the repo ships, and nothing has a secret default: a
missing token must fail loudly at startup rather than quietly authorising
everyone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _parse_tokens(raw: str) -> dict[str, str]:
    """`identity:token,identity:token` → {token: identity}.

    Keyed by token because that is the lookup direction, and because it makes
    two identities sharing a token impossible to express by accident.
    """
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        identity, _, token = pair.partition(":")
        if not identity or not token:
            raise ValueError(
                f"ZVONOK_API_TOKENS entry {pair!r} is not identity:token"
            )
        out[token] = identity
    return out


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "ZVONOK_DATABASE_URL", "postgresql://zvonok:zvonok@127.0.0.1:5432/zvonok"
    )

    # Per-client bearer tokens. Per-agent identity is what makes per-agent caps
    # and per-agent audit possible (BRIEF §5.5) — there is deliberately no
    # single shared token.
    tokens: dict[str, str] = field(
        default_factory=lambda: _parse_tokens(os.getenv("ZVONOK_API_TOKENS", ""))
    )
    # The agent worker calls back with this. Separate from client tokens: it can
    # write call outcomes but cannot place calls.
    internal_token: str = os.getenv("ZVONOK_INTERNAL_TOKEN", "")

    livekit_url: str = os.getenv("LIVEKIT_HTTP_URL", "http://127.0.0.1:7880")
    livekit_api_key: str = os.getenv("LIVEKIT_API_KEY", "")
    livekit_api_secret: str = os.getenv("LIVEKIT_API_SECRET", "")
    agent_name: str = os.getenv("ZVONOK_AGENT_NAME", "zvonok-caller")

    # Extractor (BRIEF §5.4). Decided 2026-07-27: xAI text model on the same key
    # as the voice brain — no new account, no new secret.
    xai_api_key: str = os.getenv("XAI_API_KEY", "")
    xai_base_url: str = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
    extractor_model: str = os.getenv("ZVONOK_EXTRACTOR_MODEL", "grok-latest")

    # Caps (BRIEF §6). These gate BEFORE dispatch, because the agent's own
    # MAX_CONCURRENT_CALLS is a per-worker safety net, not spend control: a
    # second worker cannot see the first one's load.
    max_duration_default: int = _int("ZVONOK_MAX_DURATION_DEFAULT", 300)
    max_duration_hard: int = _int("ZVONOK_MAX_DURATION_HARD", 600)
    max_concurrent_calls: int = _int("ZVONOK_MAX_CONCURRENT_CALLS", 2)
    daily_calls_per_identity: int = _int("ZVONOK_DAILY_CALLS", 40)
    daily_minutes_per_identity: int = _int("ZVONOK_DAILY_MINUTES", 60)
    daily_usd_per_identity: float = _float("ZVONOK_DAILY_USD", 10.0)

    # How long after a call should have finished before the janitor gives up
    # waiting for the agent's callback and reconciles the job by other means.
    reconcile_grace_seconds: int = _int("ZVONOK_RECONCILE_GRACE", 90)
    # How long a job may sit unclaimed before we accept that no worker is going
    # to take it. Short on purpose: nothing has been dialled or billed yet, and
    # every unclaimed job holds a concurrency slot that blocks real calls.
    provisioning_timeout_seconds: int = _int("ZVONOK_PROVISIONING_TIMEOUT", 45)
    # Read-only view of the agent's transcript directory. Both run on de1, so an
    # agent whose HTTP callback never lands still leaves a file we can pick up —
    # this is the fallback half of "at-least-once" (BRIEF §9 phase-2 trap 4).
    transcript_dir: str = os.getenv("ZVONOK_TRANSCRIPT_DIR", "/data/transcripts")

    webhook_secret: str = os.getenv("ZVONOK_WEBHOOK_SECRET", "")
    retention_days: int = _int("ZVONOK_RETENTION_DAYS", 180)

    def require(self) -> None:
        missing = [
            name
            for name, value in (
                ("ZVONOK_API_TOKENS", self.tokens),
                ("ZVONOK_INTERNAL_TOKEN", self.internal_token),
                ("LIVEKIT_API_KEY", self.livekit_api_key),
                ("LIVEKIT_API_SECRET", self.livekit_api_secret),
                ("XAI_API_KEY", self.xai_api_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"missing required env: {', '.join(missing)}")


settings = Settings()
