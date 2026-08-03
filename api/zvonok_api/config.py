"""Configuration, all from env (BRIEF §6: secrets live in env files on de1 only).

Nothing here reads a file the repo ships, and nothing has a secret default: a
missing token must fail loudly at startup rather than quietly authorising
everyone.

Two scopes, and the difference is load-bearing:

  TENANT   — a billing account. Its own Zadarma trunk, its own verified caller
             IDs, its own xAI key, its own agent worker. Two tenants share the
             box and nothing else. This is what makes "their calls, their money,
             their number on the callee's screen" true rather than aspirational.
  IDENTITY — one bearer token, i.e. one client agent. Caps and audit are per
             identity, so a misbehaving OpenClaw cannot spend mac-claude's daily
             budget. Several identities may belong to one tenant.

Both are expressed as env SUFFIXES: `XAI_API_KEY_FRIEND` overrides `XAI_API_KEY`
for tenant `friend`, `ZVONOK_DAILY_USD_MAC_CLAUDE` overrides `ZVONOK_DAILY_USD`
for identity `mac-claude`. An unset or empty suffixed variable falls back to the
unsuffixed one, which is what keeps a single-tenant deployment configured
exactly as it was before tenants existed — nothing to change, nothing to break.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .policy import CallerIds

logger = logging.getLogger("zvonok.config")

# The tenant every identity belongs to unless ZVONOK_TENANT_<IDENTITY> says
# otherwise. Its config is the unsuffixed env, so an existing deployment is a
# single tenant named "default" without touching a single variable.
DEFAULT_TENANT = "default"


# `or`, not os.getenv's own default: a variable set to "" is present as far as
# getenv is concerned, so `VAR=""` in .env would reach int() and raise at import
# — a service that will not start because an optional setting was blanked.
def _int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


def _float(name: str, default: float) -> float:
    return float(os.getenv(name) or default)


def _suffix(scope: str) -> str:
    """`mac-claude` → `_MAC_CLAUDE`. Env var names cannot contain hyphens."""
    return "_" + re.sub(r"[^A-Z0-9]+", "_", scope.upper())


def _scoped(var: str, scope: str, default: str = "") -> str:
    """`VAR_<SCOPE>` if set and non-empty, else `VAR`, else `default`.

    Empty-means-absent is deliberate: .env.example ships optional variables as
    `VAR=""`, so treating an empty override as an override would silently blank
    the fallback for anyone who copied the template. It applies at BOTH levels —
    an empty base variable falls through to `default` too, since `os.getenv`'s
    own default only fires when a name is absent, not when it is set to "". Get
    that wrong and emptying a numeric variable raises `int('')` at import.
    """
    return os.getenv(var + _suffix(scope)) or os.getenv(var) or default


def _own(var: str, tenant: str) -> str:
    """A tenant's OWN value, with no inheritance from the unsuffixed variable.

    Used for everything that identifies an account, and the distinction is the
    whole point of tenants rather than a nicety. A tenant that inherited the
    default tenant's xAI key would bill extraction to the wrong person; one that
    inherited its caller IDs could dial out as them, on a DID verified against
    somebody else's SIP account; one that inherited its agent_name would have its
    calls placed by the other tenant's worker, on the other tenant's trunk.

    All three are silent — the call connects and sounds fine — so they must be
    impossible to reach by forgetting a variable, not merely discouraged.
    Operational settings (base URL, model, caps) inherit via `_scoped`, because
    getting those wrong costs nothing worse than an odd default.
    """
    if tenant == DEFAULT_TENANT:
        return os.getenv(var, "")
    return os.getenv(var + _suffix(tenant), "")


def _scoped_int(var: str, scope: str, default: int) -> int:
    return int(_scoped(var, scope, str(default)))


def _scoped_float(var: str, scope: str, default: float) -> float:
    return float(_scoped(var, scope, str(default)))


def _parse_tokens(raw: str) -> dict[str, str]:
    """`identity:token,identity:token` → {token: identity}.

    Keyed by token because that is the lookup direction.

    A duplicate token is FATAL, not last-write-wins. This dict used to be built
    with a bare assignment and a comment claiming that two identities sharing a
    token was "impossible to express by accident" — it was not; the second entry
    simply overwrote the first. `a:samesecret,b:samesecret` authenticated both
    callers as `b`, so identity `a`'s calls were placed on `b`'s tenant, billed
    to `b`'s account and counted against `b`'s caps, while `a`'s own caps sat
    untouched. Every guarantee that is per-identity — caps, audit, which tenant
    pays — rests on this mapping being injective.
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
        if token in out:
            raise ValueError(
                f"ZVONOK_API_TOKENS gives identities {out[token]!r} and "
                f"{identity!r} the same token — they would be indistinguishable "
                f"at the door, and one would spend the other's budget on the "
                f"other's account. Issue a separate token per identity."
            )
        out[token] = identity
    return out


@dataclass(frozen=True)
class Tenant:
    """One billing account's own credentials and numbers.

    The Zadarma trunk is NOT here, and that is the point: each tenant's calls are
    placed by its own agent worker process, which holds the trunk id and the
    voice-brain key in its own env. Nothing account-specific has to travel
    through dispatch metadata, which is persisted in Redis and shows up in logs
    (see dispatch.dispatch_call). `agent_name` is the entire routing mechanism.
    """

    name: str
    agent_name: str
    xai_api_key: str
    xai_base_url: str
    extractor_model: str
    caller_ids: CallerIds
    # This tenant's worker authenticates its call-outcome callbacks with this.
    # Per tenant, not shared, because the internal endpoints trust it to say
    # WHOSE call is being reported on — see Settings.internal_tokens.
    internal_token: str


def _tenant(name: str) -> Tenant:
    is_default = name == DEFAULT_TENANT
    return Tenant(
        name=name,
        # .strip() to match what the worker does to the same value before it
        # registers (agent.py). Without it the two disagree about what the name
        # IS: " zvonok-caller " and "zvonok-caller" look distinct here and pass
        # the uniqueness check in require(), while both workers register under
        # the same stripped name and LiveKit routes between them at random.
        agent_name=_own("ZVONOK_AGENT_NAME", name).strip()
        or ("zvonok-caller" if is_default else ""),
        # Extraction runs HERE, in call-api, not in the worker — so unlike the
        # voice brain's key this one cannot simply live in the worker's env. A
        # tenant's transcripts must be read by a model the tenant pays for.
        xai_api_key=_own("XAI_API_KEY", name),
        xai_base_url=_scoped("XAI_BASE_URL", name, "https://api.x.ai/v1"),
        extractor_model=_scoped("ZVONOK_EXTRACTOR_MODEL", name, "grok-latest"),
        caller_ids=CallerIds.parse(
            owned=_own("ZVONOK_OWNED_CALLER_IDS", name),
            default=_own("ZVONOK_DEFAULT_CALLER_ID", name),
            by_country=_own("ZVONOK_CALLER_ID_BY_COUNTRY", name),
        ),
        # `_own`, so an added tenant cannot inherit the default tenant's — that
        # is the entire fix. A shared internal token authenticates "some worker
        # on this box" and nothing more, and the internal endpoints act on
        # whatever job id is in the URL: room names embed the job id and the
        # shared LiveKit credential can list rooms, so one tenant's worker could
        # settle another tenant's call, replace its transcript, and trigger
        # extraction against the victim's xAI key.
        internal_token=_own("ZVONOK_INTERNAL_TOKEN", name),
    )


@dataclass(frozen=True)
class IdentityLimits:
    """What one client agent may spend, and whose account pays for it."""

    identity: str
    tenant: str
    daily_calls: int
    daily_minutes: int
    daily_usd: float
    max_concurrent: int
    max_duration_default: int
    max_duration_hard: int


def _limits(identity: str) -> IdentityLimits:
    return IdentityLimits(
        identity=identity,
        # `or`, not a getenv default: docker-compose passes unset variables
        # through as empty strings, and an empty tenant name would resolve to a
        # tenant that exists in no config at all.
        tenant=os.getenv("ZVONOK_TENANT" + _suffix(identity), "") or DEFAULT_TENANT,
        daily_calls=_scoped_int("ZVONOK_DAILY_CALLS", identity, 40),
        daily_minutes=_scoped_int("ZVONOK_DAILY_MINUTES", identity, 60),
        daily_usd=_scoped_float("ZVONOK_DAILY_USD", identity, 10.0),
        # Falls back to the box-wide cap, i.e. "may fill the box alone" — the
        # pre-tenant behaviour. With more than one tenant that is no longer what
        # you want, so Settings.warnings says so at startup.
        max_concurrent=_scoped_int("ZVONOK_MAX_CONCURRENT_CALLS", identity, 2),
        max_duration_default=_scoped_int("ZVONOK_MAX_DURATION_DEFAULT", identity, 300),
        max_duration_hard=_scoped_int("ZVONOK_MAX_DURATION_HARD", identity, 600),
    )


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
    # write call outcomes but cannot place calls. Per TENANT — the mapping is
    # `internal_tokens` below; this field is only the default tenant's, kept so
    # a single-tenant .env is unchanged.
    internal_token: str = os.getenv("ZVONOK_INTERNAL_TOKEN", "")

    livekit_url: str = os.getenv("LIVEKIT_HTTP_URL", "http://127.0.0.1:7880")
    livekit_api_key: str = os.getenv("LIVEKIT_API_KEY", "")
    livekit_api_secret: str = os.getenv("LIVEKIT_API_SECRET", "")

    # Caps (BRIEF §6). These gate BEFORE dispatch, because the agent's own
    # MAX_CONCURRENT_CALLS is a per-worker safety net, not spend control: a
    # second worker cannot see the first one's load — and with two tenants there
    # now genuinely are two workers.
    #
    # This one is the BOX ceiling: de1's CPU and upstream bandwidth do not care
    # whose call it is. Per-identity shares live in IdentityLimits.
    max_concurrent_calls: int = _int("ZVONOK_MAX_CONCURRENT_CALLS", 2)

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
    # Shared across tenants: the janitor needs one place to look, and files are
    # named by job id.
    transcript_dir: str = os.getenv("ZVONOK_TRANSCRIPT_DIR", "/data/transcripts")

    webhook_secret: str = os.getenv("ZVONOK_WEBHOOK_SECRET", "")
    retention_days: int = _int("ZVONOK_RETENTION_DAYS", 180)

    # Derived from `tokens` in __post_init__; reach them through the accessors.
    limits: dict[str, IdentityLimits] = field(default_factory=dict)
    tenants: dict[str, Tenant] = field(default_factory=dict)
    internal_tokens: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        limits = {identity: _limits(identity) for identity in set(self.tokens.values())}
        tenants = {lim.tenant: _tenant(lim.tenant) for lim in limits.values()}
        warnings: list[str] = []

        if len(tenants) > 1:
            greedy = sorted(
                lim.identity
                for lim in limits.values()
                if lim.max_concurrent >= self.max_concurrent_calls
            )
            if greedy:
                warnings.append(
                    f"{len(tenants)} tenants share a box-wide concurrency cap of "
                    f"{self.max_concurrent_calls}, and {', '.join(greedy)} can fill "
                    f"it alone — set ZVONOK_MAX_CONCURRENT_CALLS_<IDENTITY> so one "
                    f"tenant cannot starve another"
                )
            # Duplicate agent_name is NOT a warning — see require(). It is the
            # one misconfiguration that silently bills the wrong account.
        # token → tenant. Duplicates are fatal for the same reason two identities
        # sharing a client token are (_parse_tokens): the mapping has to be
        # injective or the thing it identifies is not identified.
        internal: dict[str, str] = {}
        for tenant in sorted(tenants.values(), key=lambda t: t.name):
            if not tenant.internal_token:
                continue
            owner = internal.get(tenant.internal_token)
            if owner:
                raise ValueError(
                    f"tenants {owner!r} and {tenant.name!r} share an internal "
                    f"token — either could settle the other's calls and have "
                    f"the transcript extracted against the other's xAI key. "
                    f"Issue a separate ZVONOK_INTERNAL_TOKEN per tenant."
                )
            internal[tenant.internal_token] = tenant.name

        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "tenants", tenants)
        object.__setattr__(self, "internal_tokens", internal)
        object.__setattr__(self, "warnings", warnings)

    def limits_for(self, identity: str) -> IdentityLimits:
        """Caps for an identity, rebuilding from env for one we no longer hold a
        token for: a revoked client still has jobs in the table, and the janitor
        still has to finish them."""
        return self.limits.get(identity) or _limits(identity)

    def tenant_for(self, identity: str) -> Tenant:
        """Whose account pays for this identity's calls, and with whose numbers.

        Use this only when deciding about a call that has not been placed yet.
        For an existing job use `tenant_of`, which reads what was decided at
        admission instead of re-deriving it now.
        """
        name = self.limits_for(identity).tenant
        return self.tenants.get(name) or _tenant(name)

    def tenant_of(self, job: Mapping[str, Any]) -> Tenant:
        """The tenant a job was ADMITTED under, not the one its identity maps to
        today.

        Placing a call fixes the trunk and the caller ID at dispatch; the money
        and the transcript should be equally immutable, and until `jobs.tenant`
        existed they were not. Editing ZVONOK_TENANT_<IDENTITY> — or dropping the
        mapping while jobs were still unextracted — moved extraction, /reextract
        and the janitor's disk recovery onto the NEW mapping, so one tenant's
        transcript was read by, and billed to, another tenant's xAI key. The
        already-created LiveKit dispatch keeps its original agent name, so no
        in-flight call reroutes: it is the accounting that moved, silently.

        Rows written before the column existed fall back to the derived answer,
        which is the best that can be done for them and is what they got anyway.
        """
        name = job.get("tenant")
        if not name:
            return self.tenant_for(job["identity"])
        known = self.tenants.get(name)
        if known is not None:
            return known
        # A tenant that owns outstanding jobs but is no longer configured — its
        # last token was revoked, or its block was deleted from .env while calls
        # were still unextracted. `_own` does not inherit, so what comes back has
        # blank credentials and fails at extraction rather than quietly using
        # somebody else's key. That is the safe direction, but it is silent, and
        # the operator's actual mistake was removing config too early.
        logger.warning(
            "job %s belongs to tenant %r, which is no longer configured — "
            "it has no credentials, so extraction will fail until it is "
            "restored", job.get("id", "?"), name,
        )
        return _tenant(name)

    def worker_owns(self, job: Mapping[str, Any], tenant: str) -> bool:
        """May a worker authenticated as `tenant` report on this job?

        Lives here rather than in the endpoint because it is a pure statement
        about tenancy, and because an authorization rule that can only be
        exercised by standing up FastAPI, LiveKit and Postgres is a rule that
        does not get tested.
        """
        return self.tenant_of(job).name == tenant

    def tenant_for_internal_token(self, token: str) -> str | None:
        """Which tenant's worker is calling, or None if the token is unknown."""
        for known, name in self.internal_tokens.items():
            if secrets.compare_digest(token, known):
                return name
        return None

    def require(self) -> None:
        missing = [
            name
            for name, value in (
                ("ZVONOK_API_TOKENS", self.tokens),
                ("LIVEKIT_API_KEY", self.livekit_api_key),
                ("LIVEKIT_API_SECRET", self.livekit_api_secret),
            )
            if not value
        ]
        # Per tenant, because these do not inherit (see _own) and the failure
        # modes are silent: no key means transcripts that can never be extracted,
        # discovered only after the call is paid for; no agent_name means the
        # dispatch has nowhere to go, or worse, somewhere wrong.
        for tenant in sorted(self.tenants.values(), key=lambda t: t.name):
            if tenant.name == DEFAULT_TENANT:
                # Exactly the pre-tenant requirements, so an existing
                # single-tenant .env keeps starting unchanged.
                if not tenant.xai_api_key:
                    missing.append("XAI_API_KEY")
                if not tenant.agent_name:
                    missing.append("ZVONOK_AGENT_NAME")
                if not tenant.internal_token:
                    missing.append("ZVONOK_INTERNAL_TOKEN")
                continue
            own = _suffix(tenant.name)
            if not tenant.xai_api_key:
                missing.append("XAI_API_KEY" + own)
            if not tenant.agent_name:
                missing.append("ZVONOK_AGENT_NAME" + own)
            # Fatal rather than "falls back to the shared one": a tenant with no
            # internal token of its own would have its worker rejected at the
            # callback, which shows up as calls that complete on the phone and
            # never finalise in the database — expensive and baffling.
            if not tenant.internal_token:
                missing.append("ZVONOK_INTERNAL_TOKEN" + own)
            # An added tenant with no numbers of its own is fail-OPEN: calls go
            # out on whatever its trunk defaults to, priced as the expensive
            # origin group, and its clients cannot request a caller ID at all.
            # Nothing about that is a deliberate configuration, and
            # .env.example promises it fails here.
            if not tenant.caller_ids.owned:
                missing.append("ZVONOK_OWNED_CALLER_IDS" + own)
            if not tenant.caller_ids.default:
                missing.append("ZVONOK_DEFAULT_CALLER_ID" + own)
        if missing:
            raise RuntimeError(f"missing required env: {', '.join(missing)}")

        # A default caller ID that is not on its own verified list cannot be
        # requested explicitly by a client and prices as the expensive origin
        # group — always a typo. Fatal only for added tenants: the default
        # tenant's config predates this check and must keep starting.
        for tenant in sorted(self.tenants.values(), key=lambda t: t.name):
            ids = tenant.caller_ids
            if tenant.name == DEFAULT_TENANT or not ids.owned:
                continue
            unverified = [
                did for did in [ids.default, *ids.by_country.values()]
                if did and did not in ids.owned
            ]
            if unverified:
                raise RuntimeError(
                    f"tenant {tenant.name} routes to {', '.join(sorted(set(unverified)))} "
                    f"but ZVONOK_OWNED_CALLER_IDS{_suffix(tenant.name)} does not list "
                    f"{'them' if len(set(unverified)) > 1 else 'it'}"
                )

        # Fatal, not a warning. Two tenants under one agent_name means LiveKit
        # hands each dispatch to whichever worker is free, so roughly half of one
        # tenant's calls go out on the OTHER tenant's Zadarma trunk, presenting
        # the other tenant's caller ID and billed to their balance. Nothing about
        # the call looks wrong — it connects, the audio is fine, the transcript
        # arrives — so there is no operational signal that would ever surface it.
        # A service that refuses to start is the only honest failure mode.
        # `_suffix` is not injective: it collapses every run of non-alphanumeric
        # characters to one underscore, so `friend-a` and `friend_a` both read
        # ZVONOK_TENANT_FRIEND_A, and tenants `acme-prod` and `acme_prod` read
        # the same account credentials. Two names that look distinct in
        # ZVONOK_API_TOKENS would then share a trunk, a key and a budget.
        for kind, names in (
            ("identities", sorted(set(self.tokens.values()))),
            ("tenants", sorted(self.tenants)),
        ):
            seen: dict[str, str] = {}
            for name in names:
                first = seen.setdefault(_suffix(name), name)
                if first != name:
                    raise RuntimeError(
                        f"{kind} {first!r} and {name!r} both map to env suffix "
                        f"{_suffix(name)}, so they would share configuration "
                        f"that is meant to separate them — rename one"
                    )

        by_name: dict[str, list[str]] = {}
        for tenant in self.tenants.values():
            by_name.setdefault(tenant.agent_name, []).append(tenant.name)
        clashes = {name: sorted(t) for name, t in by_name.items() if len(t) > 1}
        if clashes:
            detail = "; ".join(
                f"{name!r} claimed by {' and '.join(tenants)}"
                for name, tenants in sorted(clashes.items())
            )
            raise RuntimeError(
                f"tenants must not share an agent_name: {detail}. Set "
                f"ZVONOK_AGENT_NAME_<TENANT> per tenant — calls would otherwise "
                f"be placed on whichever worker was free, on the wrong trunk"
            )


settings = Settings()
