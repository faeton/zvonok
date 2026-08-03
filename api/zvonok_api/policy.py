"""Destination policy, caller-ID selection, cost estimation, disclosure level.

This is the module that decides whether we are allowed to spend money on a
number, and how much it will probably cost. Everything here is default-deny
(BRIEF §6): an unrecognised prefix is refused, not dialled.

Nothing in here consults the callee. All inputs are our own — the requesting
agent's number and goal — which is why keyword matching is acceptable in
`disclosure_level_for` and would not be in the voice agent.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# E.164
# ---------------------------------------------------------------------------

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalise_number(raw: str) -> str:
    """Strip formatting and require strict E.164.

    The 8-digit minimum is not cosmetic: it is what keeps emergency and short
    service codes (112, 999, 911, 118xx) from ever reaching the dialler, since
    none of them can be expressed as a valid international number.
    """
    cleaned = re.sub(r"[\s\-().]", "", raw or "")
    if not cleaned.startswith("+") and cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not _E164.match(cleaned):
        raise PolicyError(
            f"{raw!r} is not a valid E.164 number — expected +<country><number>, "
            f"8–15 digits"
        )
    return cleaned


class PolicyError(ValueError):
    """Rejected before any money could be spent. Always safe to show a client."""


# ---------------------------------------------------------------------------
# Country resolution
# ---------------------------------------------------------------------------

# Longest-prefix wins, so 380 beats 38 and 44 beats 4. Only countries we are
# prepared to call are listed — an unknown prefix has no entry and is therefore
# refused by `check_destination`, which is the intended behaviour.
_PREFIXES: dict[str, str] = {
    "1": "US",     # NANP; the Caribbean NPAs inside it are denied below
    "33": "FR",
    "34": "ES",
    "39": "IT",
    "41": "CH",
    "44": "GB",
    "48": "PL",
    "49": "DE",
    "91": "IN",
    "357": "CY",
    "380": "UA",
    "966": "SA",
    "971": "AE",
    "977": "NP",
}

# BRIEF §6 allowlist, plus US (verified €0/min and used for the phase-1
# acceptance call) and CY (we own a Cypriot caller ID).
ALLOWED_COUNTRIES = frozenset(
    {"ES", "GB", "UA", "PL", "DE", "FR", "IT", "CH", "SA", "IN", "NP", "AE", "US", "CY"}
)

# NANP area codes that look domestic but bill like premium international. These
# are the classic one-ring / wangiri scam destinations, and +1 is exactly the
# prefix where a naive country check would wave them through.
_NANP_DENIED_NPA = frozenset({
    "242", "246", "264", "268", "284", "340", "345", "441", "473", "649",
    "664", "670", "671", "684", "721", "758", "767", "784", "787", "809",
    "829", "849", "868", "869", "876", "939",
    "900", "976",  # US premium-rate
})

# Premium-rate and special-tariff ranges inside otherwise-allowed countries.
# The country allowlist does nothing about these — a +44 number can still be a
# £3.60/min service line.
_PREMIUM_PREFIXES: tuple[str, ...] = (
    "+449", "+4470", "+44842", "+44843", "+44844", "+44845",
    "+44870", "+44871", "+44872", "+44873",
    "+34803", "+34806", "+34807", "+34905", "+34907",
    "+49900", "+49137", "+49180",
    "+3389", "+33899",
    "+39144", "+39166", "+39899",
    "+4870",
    "+41900", "+41901", "+41906",
    "+380900",
    # Satellite ranges — no allowed country owns them, but spell it out so the
    # refusal message is useful rather than "unknown prefix".
    "+870", "+881", "+882", "+883", "+888",
)


def country_for(number: str) -> str | None:
    digits = number.lstrip("+")
    for length in (3, 2, 1):
        if len(digits) > length and digits[:length] in _PREFIXES:
            return _PREFIXES[digits[:length]]
    return None


# ---------------------------------------------------------------------------
# Caller ID (BRIEF §5.1, §9 phase-0)
# ---------------------------------------------------------------------------

# The account's actual numbers are deployment data, not code: they live in env
# (same rule as secrets — see .env.example).
#
#   ZVONOK_OWNED_CALLER_IDS      comma-separated E.164, every DID verified on
#                                the SIP account
#   ZVONOK_DEFAULT_CALLER_ID     used when no per-country rule matches; the
#                                cheap origin for most destinations
#   ZVONOK_CALLER_ID_BY_COUNTRY  "UA:+380...,CY:+357..." per-country overrides
#
# These are PER TENANT (config.Tenant reads them with a `_<TENANT>` suffix),
# because a DID is verified against one specific SIP account: tenant A cannot
# present tenant B's number, and must not be able to try. The module-level
# `DEFAULT_CALLER_IDS` below is the unsuffixed set — the single-tenant
# configuration, unchanged.
#
# Caller ID is a COST lever before it is an answer-rate one: measured ×20–34
# price swings on EU/UK destinations depending on the origin group (BRIEF §9
# phase-0).


def _split_csv(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


@dataclass(frozen=True)
class CallerIds:
    """One tenant's verified numbers and how to pick between them.

    Passed explicitly rather than read from module state so that two tenants can
    coexist in one process. Every function that prices or validates a caller ID
    takes one of these; the parameter is optional only so the single-tenant call
    sites and the tests keep working against the unsuffixed env.
    """

    owned: frozenset[str]
    default: str
    by_country: dict[str, str]

    @classmethod
    def parse(cls, owned: str, default: str, by_country: str) -> CallerIds:
        mapping: dict[str, str] = {}
        for pair in _split_csv(by_country):
            country, _, did = pair.partition(":")
            if country and did:
                mapping[country.strip().upper()] = did.strip()
        return cls(
            owned=frozenset(_split_csv(owned)),
            default=(default or "").strip(),
            by_country=mapping,
        )


# A caller ID we do not own is typically not rejected by the SIP provider — it
# silently falls back to the account default, which is confusing to debug and
# bills at the wrong origin rate. So validate ours here instead of discovering
# it in an invoice.
DEFAULT_CALLER_IDS = CallerIds.parse(
    owned=os.getenv("ZVONOK_OWNED_CALLER_IDS", ""),
    default=os.getenv("ZVONOK_DEFAULT_CALLER_ID", ""),
    by_country=os.getenv("ZVONOK_CALLER_ID_BY_COUNTRY", ""),
)


def caller_id_for(
    country: str | None,
    override: str | None = None,
    ids: CallerIds | None = None,
) -> str:
    ids = ids or DEFAULT_CALLER_IDS
    if override:
        # Scoped to THIS tenant's verified list, which is what stops one tenant
        # dialling out as another (they share a box, not an account). The error
        # names only the caller's own numbers — the other tenant's DIDs are not
        # this client's business.
        if override not in ids.owned:
            raise PolicyError(
                f"caller_id {override} is not on the verified list "
                f"(ZVONOK_OWNED_CALLER_IDS); owned: "
                f"{', '.join(sorted(ids.owned)) or 'none configured'}"
            )
        return override
    return ids.by_country.get(country or "", ids.default)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

# Price depends on the ORIGIN as much as the destination — phase 0 measured a
# ×20–34 swing on EU/UK routes between an EU/UK caller ID and a UA one (BRIEF §9
# phase-0). An estimator keyed on destination alone therefore under-reports by an
# order of magnitude exactly when a client overrides `caller_id`, which is the
# case where a daily spend cap most needs to be right.
#
# €/min, keyed (destination country, origin group). US and ES/GB are measured;
# the rest are deliberately pessimistic so an unmeasured route cannot quietly
# blow through a cap. Phase 3 replaces this with reconciliation against Zadarma
# /v1/statistics/ (BRIEF §7.1).
_EUR_PER_MIN: dict[tuple[str, str], float] = {
    ("US", "eu"): 0.00, ("US", "ua"): 0.00,   # measured: €0 for every caller ID
    ("GB", "eu"): 0.018, ("GB", "ua"): 0.50,  # measured both ways
    ("ES", "eu"): 0.022, ("ES", "ua"): 0.44,  # measured both ways
    ("UA", "eu"): 0.19, ("UA", "ua"): 0.19,   # measured: caller-ID-independent
}
# A UA-group origin into an unmeasured destination is the shape that produced
# the ×20–34 surprises, so it is priced as if it will.
_EUR_PER_MIN_UNMEASURED = {"eu": 0.12, "ua": 0.50}
EUR_USD = 1.08  # the whole figure is an estimate; a fixed rate is honest enough

# xAI public list price (ACCOUNTS.md §2 — from docs, not from an invoice yet).
MODEL_USD_PER_MIN = 0.05


def origin_group(caller_id: str | None, ids: CallerIds | None = None) -> str:
    """Which pricing group our caller ID falls into.

    Derived from the caller ID's own country prefix: Ukrainian DIDs price as
    the expensive "ua" origin, the rest as "eu" — but only for numbers on the
    verified list. Anything unknown (including no caller ID at all, i.e. the
    trunk default) is priced pessimistically, so a gap in configuration cannot
    quietly under-report against the spend cap. That is also why passing the
    WRONG tenant's list here is safe: an unrecognised DID prices high, never low.
    """
    ids = ids or DEFAULT_CALLER_IDS
    if not caller_id or caller_id not in ids.owned:
        return "ua"
    return "ua" if caller_id.startswith("+380") else "eu"


def estimate_usd(
    country: str | None,
    seconds: float,
    caller_id: str | None = None,
    ids: CallerIds | None = None,
) -> float:
    ids = ids or DEFAULT_CALLER_IDS
    minutes = max(seconds, 0.0) / 60.0
    group = origin_group(caller_id or ids.default, ids)
    rate = _EUR_PER_MIN.get(
        (country or "", group), _EUR_PER_MIN_UNMEASURED[group]
    )
    return round(rate * minutes * EUR_USD + MODEL_USD_PER_MIN * minutes, 4)


# ---------------------------------------------------------------------------
# Disclosure level (BRIEF §8.1)
# ---------------------------------------------------------------------------

# Jurisdictions in our allowlist with the strictest rules about recording the
# other party. Not legal advice — the point is that the default lands on the
# safe side without a lawyer in the loop for every new destination.
_FULL_DISCLOSURE_COUNTRIES = frozenset({"DE", "CH", "PL"})

# Goals that commit or book something on someone's behalf get the explicit
# transcribe-and-store wording. Keyword matching is acceptable here — unlike in
# the voice agent, where classification must go by turn shape rather than word
# lists — because the goal is OUR text, written by the requesting agent, and the
# failure mode is asymmetric: a false positive only makes the disclosure more
# explicit than it needed to be.
#
# ⚠ This list is a BACKSTOP and is necessarily incomplete: it cannot cover every
# way to express a commitment in four languages, let alone a goal phrased
# obliquely ("tell them I'll take it"). What actually keeps `brief` honest is
# that it is never selected automatically — a requester has to ask for it, for a
# call they know is a one-question canvass. Treat a miss here as a gap in a
# safety net, not as a hole in the only defence.
_COMMITMENT_WORDS = (
    # en
    "book", "booking", "reserve", "reservation", "cancel", "order", "buy",
    "schedule", "appointment", "confirm my", "on my behalf", "sign up",
    "sign me up", "put me down", "table for",
    # ru
    "забронир", "бронир", "заказ", "отмен", "запиш", "записать", "запись",
    # es
    "reserva", "reservar", "cancelar", "pedido", "pedir", "cita", "comprar",
    # pl — added after review found the list had no Polish at all, while the
    # only canvass we have ever run was Polish. "zarezerwuj stolik" and "zamów
    # dwie pizze" both sailed through the gate.
    "rezerw", "zarezerw", "zamów", "zamow", "zamaw", "anuluj", "odwoła",
    "odwola", "umów", "umow", "wizyt", "zakup", "kupić", "kupic",
)


def disclosure_level_for(
    country: str | None, goal: str, override: str | None = None
) -> str:
    """Pick the disclosure register. An explicit request wins — except upward.

    "brief" is only ever reachable by explicit request (see CallRequest), and it
    is the one level that is genuinely LESS than §8's baseline: it drops the
    principal entirely, leaving only "I am an AI" and "your answer is kept".

    That makes an unguarded override a real hole rather than a theoretical one:
    a playbook file could set `"disclosure_level": "brief"` and quietly canvass
    its way through a booking in a two-party-consent country. So a commitment —
    booking, ordering, cancelling on someone's behalf — REFUSES brief and takes
    the full text instead. Nobody commits anything in someone's name behind the
    shortest disclosure we have.

    Note the asymmetry, which is deliberate: a caller may always ask for MORE
    disclosure than we would choose, never less than the goal warrants. The
    country table is not applied to a `brief` request for a non-committal call:
    the requester knows it is a fifteen-second stock check, and we only know
    where the call is going.
    """
    low = (goal or "").lower()
    commits = any(w in low for w in _COMMITMENT_WORDS)

    if override == "brief" and commits:
        return "full"
    if override in ("brief", "light", "full"):
        return override
    if country in _FULL_DISCLOSURE_COUNTRIES:
        return "full"
    if commits:
        return "full"
    return "light"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Destination:
    number: str
    country: str
    caller_id: str


def check_destination(
    raw_number: str,
    caller_id_override: str | None = None,
    ids: CallerIds | None = None,
) -> Destination:
    """Validate and price a destination, or raise PolicyError.

    Order matters: cheapest and most certain refusals first, so the error a
    client sees names the actual problem rather than a downstream symptom.
    """
    number = normalise_number(raw_number)

    for prefix in _PREMIUM_PREFIXES:
        if number.startswith(prefix):
            raise PolicyError(
                f"{number} is in a premium-rate or satellite range ({prefix}) — "
                f"refused by policy"
            )

    country = country_for(number)
    if country is None:
        raise PolicyError(
            f"{number} has no recognised country prefix; destinations are "
            f"default-deny (allowed: {', '.join(sorted(ALLOWED_COUNTRIES))})"
        )

    # Area code is number[2:5]: index 0 is "+", index 1 is the country code "1".
    if country == "US" and number[2:5] in _NANP_DENIED_NPA:
        raise PolicyError(
            f"{number} is a +1 area code ({number[2:5]}) that bills as premium "
            f"or Caribbean international — refused by policy"
        )

    if country not in ALLOWED_COUNTRIES:
        raise PolicyError(
            f"destination country {country} is not on the allowlist "
            f"({', '.join(sorted(ALLOWED_COUNTRIES))})"
        )

    return Destination(
        number=number,
        country=country,
        caller_id=caller_id_for(country, caller_id_override, ids),
    )
