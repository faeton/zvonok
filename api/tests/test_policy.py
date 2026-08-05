"""Checks for the parts of call-api that decide whether to spend money.

Plain asserts, no test framework — `python3 tests/test_policy.py` from `api/`.
These are the functions where a bug is expensive rather than annoying: the
allowlist, the premium-rate refusals, the agent-dialect normalisation, and the
injection-boundary rendering.

The off-by-one that let +1900 premium numbers and Caribbean area codes through
was found here, not in review.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Caller IDs are deployment data read from env at import time. These are
# fictional test numbers (the +44 7700 900xxx block is reserved for fiction).
# Hard assignments, not setdefault: a machine that also runs a real deployment
# must not leak its actual configuration into the expectations below.
UK_DID, UA_DID, CY_DID = "+447700900123", "+380670000000", "+35799000000"
os.environ["ZVONOK_OWNED_CALLER_IDS"] = f"{UK_DID},{UA_DID},{CY_DID}"
os.environ["ZVONOK_DEFAULT_CALLER_ID"] = UK_DID
os.environ["ZVONOK_CALLER_ID_BY_COUNTRY"] = f"UA:{UA_DID},CY:{CY_DID}"

from zvonok_api import config, extractor, policy, states  # noqa: E402

failures: list[str] = []


def expect(desc: str, condition: bool) -> None:
    if condition:
        print(f"  ok   {desc}")
    else:
        print(f"  FAIL {desc}")
        failures.append(desc)


def allowed(number: str, *, caller_id: str | None = None) -> policy.Destination | None:
    try:
        return policy.check_destination(number, caller_id)
    except policy.PolicyError:
        return None


def test_destinations() -> None:
    print("destinations")
    expect("ES mobile allowed", allowed("+34600123456") is not None)
    expect("UK landline allowed", allowed("+442071234567") is not None)
    expect("US mobile allowed", allowed("+15551234567") is not None)
    expect("formatting stripped", allowed("+34 (600) 123-456") is not None)
    expect("00 prefix normalised", allowed("0034600123456") is not None)

    expect("emergency 112 refused", allowed("112") is None)
    expect("emergency 999 refused", allowed("999") is None)
    expect("too short refused", allowed("+3460012") is None)
    expect("UK premium 09 refused", allowed("+449001234567") is None)
    expect("UK 0870 service refused", allowed("+448701234567") is None)
    expect("ES premium 806 refused", allowed("+34806123456") is None)
    expect("US premium 900 refused", allowed("+19001234567") is None)
    expect("US premium 976 refused", allowed("+19761234567") is None)
    expect("Caribbean 809 refused", allowed("+18091234567") is None)
    expect("Caribbean 876 refused", allowed("+18761234567") is None)
    expect("satellite 881 refused", allowed("+881612345678") is None)
    expect("non-allowlisted country refused", allowed("+2348012345678") is None)


def test_caller_id() -> None:
    print("caller id")
    es = allowed("+34600123456")
    ua = allowed("+380671234567")
    expect("EU/UK destination gets the UK DID", es is not None and es.caller_id == UK_DID)
    expect("UA destination gets the Kyivstar DID", ua is not None and ua.caller_id == UA_DID)
    expect("unowned caller_id refused", allowed("+34600123456", caller_id="+34910000000") is None)
    owned = allowed("+34600123456", caller_id=CY_DID)
    expect("owned caller_id honoured", owned is not None and owned.caller_id == CY_DID)


def test_tenant_isolation() -> None:
    """One box, two Zadarma accounts: neither may dial out as the other.

    A DID is verified against one specific SIP account, so presenting another
    tenant's number is not merely rude — it is a call the carrier bills to the
    wrong account, with a callback number pointing at someone who never agreed
    to receive it. This is the check that would fail first if caller-ID config
    ever went back to being module-level state shared by the whole process.
    """
    print("tenant isolation")
    PL_DID = "+48512000000"
    ours = policy.CallerIds.parse(
        owned=f"{UK_DID},{UA_DID},{CY_DID}", default=UK_DID,
        by_country=f"UA:{UA_DID},CY:{CY_DID}",
    )
    theirs = policy.CallerIds.parse(owned=PL_DID, default=PL_DID, by_country="")

    def dial(number: str, ids: policy.CallerIds, caller_id: str | None = None):
        try:
            return policy.check_destination(number, caller_id, ids)
        except policy.PolicyError:
            return None

    expect("our default is ours", (d := dial("+34600123456", ours)) and d.caller_id == UK_DID)
    expect("their default is theirs", (d := dial("+34600123456", theirs)) and d.caller_id == PL_DID)
    expect(
        "we cannot present their DID",
        dial("+34600123456", ours, PL_DID) is None,
    )
    expect(
        "they cannot present our DID",
        dial("+34600123456", theirs, UK_DID) is None,
    )
    # The refusal names the caller's OWN numbers, to be useful, and nothing
    # else. Echoing back the rejected value is fine — the client sent it. What
    # would leak is the other tenant's list, and this error text goes verbatim
    # to a client agent, so the scoping has to hold here and not just in the
    # accept path.
    try:
        policy.check_destination("+34600123456", UK_DID, theirs)
        refusal = ""
    except policy.PolicyError as e:
        refusal = str(e)
    expect("refusal happened", refusal != "")
    expect(
        "refusal lists only the caller's own numbers",
        PL_DID in refusal and UA_DID not in refusal and CY_DID not in refusal,
    )

    # Pricing follows the tenant too: an unrecognised DID must price as the
    # EXPENSIVE origin, so a config gap can only ever over-report against a cap.
    expect(
        "their DID prices pessimistically under our list",
        policy.estimate_usd("GB", 60, PL_DID, ours)
        > policy.estimate_usd("GB", 60, UK_DID, ours),
    )
    expect(
        "their DID prices as EU under their own list",
        policy.estimate_usd("GB", 60, PL_DID, theirs)
        < policy.estimate_usd("GB", 60, PL_DID, ours),
    )


def test_tenant_config() -> None:
    """Env → tenants. The mapping that decides whose account pays.

    Every assertion here is a way to bill the wrong person or dial out on the
    wrong trunk, which is why this is tested rather than left to a deploy-day
    read-through of .env.
    """
    print("tenant config")
    PL_DID = "+48512000000"
    def settings() -> config.Settings:
        """Fresh Settings with the non-tenant requirements satisfied.

        Passed as arguments rather than env because those fields are plain
        dataclass defaults, evaluated once when the module is imported — only
        the tenant/identity maps are rebuilt per instance. Supplying them here
        lets require() reach the tenant checks instead of short-circuiting on
        base config the test does not care about.
        """
        return config.Settings(
            internal_token="i", livekit_api_key="k", livekit_api_secret="s"
        )

    env = {
        "ZVONOK_API_TOKENS": "mac-claude:t1,openclaw:t2,friend:t3",
        "ZVONOK_TENANT_FRIEND": "friend",
        "XAI_API_KEY": "ours",
        "XAI_API_KEY_FRIEND": "theirs",
        "ZVONOK_AGENT_NAME_FRIEND": "zvonok-caller-friend",
        "ZVONOK_OWNED_CALLER_IDS": UK_DID,
        "ZVONOK_OWNED_CALLER_IDS_FRIEND": PL_DID,
        "ZVONOK_INTERNAL_TOKEN": "internal-ours",
        "ZVONOK_INTERNAL_TOKEN_FRIEND": "internal-theirs",
        "ZVONOK_DEFAULT_CALLER_ID_FRIEND": PL_DID,
        "ZVONOK_DAILY_USD": "10",
        "ZVONOK_DAILY_USD_FRIEND": "3",
        "ZVONOK_MAX_CONCURRENT_CALLS": "2",
        # Empty means ABSENT, not "override with empty": .env.example ships
        # optional variables as VAR="", and treating that as an override would
        # blank the fallback for anyone who copied the template.
        "ZVONOK_EXTRACTOR_MODEL_FRIEND": "",
        "ZVONOK_EXTRACTOR_MODEL": "grok-4",
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        s = settings()
        ours, theirs = s.tenant_for("mac-claude"), s.tenant_for("friend")

        expect("my identities share one tenant",
               ours.name == s.tenant_for("openclaw").name == config.DEFAULT_TENANT)
        expect("their identity is its own tenant", theirs.name == "friend")
        expect("unsuffixed env is the default tenant's", ours.xai_api_key == "ours")
        expect("suffixed env overrides for that tenant", theirs.xai_api_key == "theirs")
        expect("empty override falls back", theirs.extractor_model == "grok-4")
        expect("workers are distinct", ours.agent_name != theirs.agent_name)
        expect("their DIDs are not mine", theirs.caller_ids.owned == frozenset({PL_DID}))
        expect("my DIDs are not theirs", ours.caller_ids.owned == frozenset({UK_DID}))

        # The internal token says WHOSE call a worker is reporting on, so it
        # must not inherit: a shared one lets either worker settle the other's
        # calls and have the transcript extracted against the other's xAI key.
        expect("internal tokens are per tenant",
               s.tenant_for_internal_token("internal-ours") == config.DEFAULT_TENANT
               and s.tenant_for_internal_token("internal-theirs") == "friend")
        expect("an unknown internal token maps nowhere",
               s.tenant_for_internal_token("nope") is None)

        os.environ["ZVONOK_INTERNAL_TOKEN_FRIEND"] = ""
        try:
            settings().require()
            no_token = ""
        except RuntimeError as e:
            no_token = str(e)
        expect("a tenant with no internal token fails startup",
               no_token.startswith("missing required env")
               and "ZVONOK_INTERNAL_TOKEN_FRIEND" in no_token)

        # Sharing one is worse than forgetting one, because it works.
        os.environ["ZVONOK_INTERNAL_TOKEN_FRIEND"] = "internal-ours"
        try:
            settings()
            shared = ""
        except ValueError as e:
            shared = str(e)
        expect("two tenants sharing an internal token is fatal",
               "internal token" in shared
               and "friend" in shared and config.DEFAULT_TENANT in shared)
        os.environ["ZVONOK_INTERNAL_TOKEN_FRIEND"] = "internal-theirs"

        # A job's tenant is what it was ADMITTED under, not what its identity
        # maps to today. Placing a call fixes the trunk at dispatch; the money
        # and the transcript have to be equally immutable, or re-pointing
        # ZVONOK_TENANT_<IDENTITY> hands one tenant's transcript to another
        # tenant's xAI key — silently, since the call itself never reroutes.
        moved = {"identity": "mac-claude", "tenant": "friend"}
        expect("a stored tenant wins over today's mapping",
               s.tenant_of(moved).name == "friend"
               and s.tenant_of(moved).xai_api_key == "theirs")
        expect("a row written before the column falls back to derived",
               s.tenant_of({"identity": "friend"}) == s.tenant_for("friend"))
        expect("tenant_for still answers for a call not yet placed",
               s.tenant_for("mac-claude").name == config.DEFAULT_TENANT)

        # The rule the internal endpoints enforce. Before this, a worker's
        # token proved only "some worker on this box" while the endpoint acted
        # on whatever job id was in the URL — so one tenant's worker could
        # settle another's call and have the transcript extracted against the
        # victim's key.
        theirs_job = {"id": "c_1", "identity": "friend", "tenant": "friend"}
        ours_job = {"id": "c_2", "identity": "mac-claude", "tenant": config.DEFAULT_TENANT}
        expect("a worker may report on its own tenant's job",
               s.worker_owns(theirs_job, "friend")
               and s.worker_owns(ours_job, config.DEFAULT_TENANT))
        expect("a worker may NOT report on another tenant's job",
               not s.worker_owns(theirs_job, config.DEFAULT_TENANT)
               and not s.worker_owns(ours_job, "friend"))
        # A legacy row still resolves, and still refuses the wrong worker.
        legacy = {"id": "c_3", "identity": "friend"}
        expect("a legacy row is still owned by the right tenant",
               s.worker_owns(legacy, "friend")
               and not s.worker_owns(legacy, config.DEFAULT_TENANT))
        # A stored tenant nobody configured must not resolve to somebody else's
        # credentials — it gets a blank tenant that fails at extraction.
        orphan = s.tenant_of({"id": "c_4", "identity": "friend", "tenant": "gone"})
        expect("an unconfigured stored tenant gets no credentials",
               orphan.name == "gone" and not orphan.xai_api_key)

        expect("per-identity cap applies", s.limits_for("friend").daily_usd == 3.0)
        expect("unsuffixed cap still applies", s.limits_for("mac-claude").daily_usd == 10.0)
        expect("caps stay per identity, not per tenant",
               s.limits_for("openclaw").daily_usd == 10.0)

        # Nobody set a per-identity concurrency share, so each of them can fill
        # the box alone. Legal, but it means one tenant can starve the other.
        expect("unshared concurrency is warned about",
               any("starve" in w for w in s.warnings))

        # Two tenants under one agent_name is the most expensive single typo in
        # the system: LiveKit hands each dispatch to whichever worker is free,
        # so roughly half of one tenant's calls leave on the other's trunk with
        # the other's caller ID. It connects, it sounds normal, and nothing in
        # the transcript or the job row records that it happened.
        os.environ["ZVONOK_AGENT_NAME_FRIEND"] = "zvonok-caller"
        try:
            settings().require()
            clash = ""
        except RuntimeError as e:
            clash = str(e)
        expect("a duplicate agent_name fails startup", "agent_name" in clash)
        expect("the clash names both tenants",
               "friend" in clash and config.DEFAULT_TENANT in clash)
        # Whitespace is the version of that typo which survives a careful read
        # of .env. The worker strips the value before registering, so if the API
        # compared the raw strings these two would look distinct here and
        # identical to LiveKit — the uniqueness check above would pass and the
        # calls would still cross accounts.
        os.environ["ZVONOK_AGENT_NAME_FRIEND"] = "  zvonok-caller  "
        try:
            settings().require()
            padded = ""
        except RuntimeError as e:
            padded = str(e)
        expect("a whitespace-padded duplicate is still a duplicate",
               "agent_name" in padded)

        os.environ["ZVONOK_AGENT_NAME_FRIEND"] = "zvonok-caller-friend"
        expect("distinct agent names start fine",
               settings().require() is None)

        # Env suffixes collapse punctuation, so two identities that differ only
        # in a hyphen vs an underscore would read the same ZVONOK_TENANT_* and
        # the same caps — sharing a budget and an account while appearing
        # separate in ZVONOK_API_TOKENS.
        os.environ["ZVONOK_API_TOKENS"] = "mac-claude:t1,friend:t3,friend-x:t4,friend_x:t5"
        try:
            settings().require()
            collision = ""
        except RuntimeError as e:
            collision = str(e)
        expect("identities colliding on one env suffix fail startup",
               "suffix" in collision and "friend-x" in collision)
        os.environ["ZVONOK_API_TOKENS"] = env["ZVONOK_API_TOKENS"]

        # The one that matters most: account-identifying settings must NOT be
        # inheritable. An operator who adds a tenant and forgets a variable has
        # to get an error, never a working call billed to the wrong account with
        # the wrong number on the callee's screen — that failure is silent, and
        # the call sounds completely normal while it happens.
        for var in ("XAI_API_KEY_FRIEND", "ZVONOK_OWNED_CALLER_IDS_FRIEND"):
            os.environ.pop(var, None)
        s = settings()
        theirs = s.tenant_for("friend")
        expect("a forgotten key is not inherited", theirs.xai_api_key == "")
        expect("forgotten caller IDs are not inherited", not theirs.caller_ids.owned)
        expect("my DIDs are still mine", s.tenant_for("mac-claude").caller_ids.owned)
        try:
            s.require()
            complained = ""
        except RuntimeError as e:
            complained = str(e)
        expect("a tenant without its own key fails startup",
               "XAI_API_KEY_FRIEND" in complained)
        # Fail-closed, not a warning: an added tenant with no numbers dials out
        # on whatever its trunk defaults to, and .env.example promises this stops
        # startup rather than quietly costing the expensive origin rate.
        expect("a tenant without its own caller IDs fails startup",
               "ZVONOK_OWNED_CALLER_IDS_FRIEND" in complained)

        # A default DID missing from its own verified list is always a typo: no
        # client can request it and everything it dials prices pessimistically.
        os.environ["ZVONOK_OWNED_CALLER_IDS_FRIEND"] = PL_DID
        os.environ["XAI_API_KEY_FRIEND"] = "theirs"
        os.environ["ZVONOK_DEFAULT_CALLER_ID_FRIEND"] = "+48512000999"
        try:
            settings().require()
            unverified = ""
        except RuntimeError as e:
            unverified = str(e)
        expect("a default DID not on the tenant's own list fails startup",
               "+48512000999" in unverified)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_disclosure() -> None:
    print("disclosure level")
    # The default is `brief`, and it was measured. Defaulting to `light` put an
    # eleven-second recital in front of calls whose answered legs were 6-16
    # seconds (Zadarma CDR, 2026-08-04): every callee hung up during the
    # introduction and not one heard the question. `brief` still carries both §8
    # facts; what it drops is the principal, which §8 does not require.
    expect("a plain info call is brief",
           policy.disclosure_level_for("ES", "Ask about parking") == "brief")
    expect("a plain info call in FR is brief",
           policy.disclosure_level_for("FR", "Ask if they have it in stock") == "brief")
    # The two carve-outs that are not ours to trade away.
    expect("any call in DE is full", policy.disclosure_level_for("DE", "Ask about parking") == "full")
    expect("any call in CH is full", policy.disclosure_level_for("CH", "Ask about parking") == "full")
    expect("any call in PL is full", policy.disclosure_level_for("PL", "Ask about parking") == "full")
    expect("booking is full", policy.disclosure_level_for("ES", "Book a table for 2 at 20:00") == "full")
    expect("ru booking is full", policy.disclosure_level_for("ES", "Забронировать столик") == "full")
    # ⚠ This test used to assert the opposite — that an explicit "light" won
    # here — and in doing so it blessed the hole: a booking, in a two-party
    # consent country, behind the shorter wording. Only "brief" was being
    # upgraded on a commitment, because "brief" is the conspicuous level.
    expect("a commitment outranks an explicit light",
           policy.disclosure_level_for("DE", "Book a table", "light") == "full")
    expect("a commitment outranks an explicit brief",
           policy.disclosure_level_for("ES", "Book a table", "brief") == "full")
    expect("an explicit override still wins on a plain question",
           policy.disclosure_level_for("DE", "Ask about parking", "light") == "light")
    # ⚠ Measured, not hypothetical. This exact goal wording took a Valencia call
    # to `full` on 2026-08-05: "order" matched as a substring of "in this
    # order", the callee got the eleven-second recital and hung up at 17s
    # without hearing a question. Telling the agent HOW to ask is not a
    # commitment to buy.
    for ordinal in ("Ask, in this order, one at a time: do they have it?",
                    "In order to check stock, ask whether they have it",
                    "Ask these in the order given",
                    "Read them back in reverse order"):
        expect(f"ordinal 'order' is not a commitment: {ordinal[:28]!r}",
               policy.disclosure_level_for("ES", ordinal) == "brief")
    # ...without going deaf to the real thing.
    for real in ("Order two pizzas for delivery",
                 "Place an order for four of them",
                 "Ask them to order it in for us"):
        expect(f"a real order is still a commitment: {real[:24]!r}",
               policy.disclosure_level_for("ES", real) == "full")
    # `brief` drops the principal entirely, so it is the one override that must
    # not be able to go under the floor. A playbook file asking for brief on a
    # booking would otherwise commit something in someone's name behind the
    # shortest disclosure we have.
    expect("brief is honoured for a plain question",
           policy.disclosure_level_for("PL", "Ask whether they stock Ozempic 2 mg", "brief") == "brief")
    expect("brief is refused for a booking",
           policy.disclosure_level_for("PL", "Book a table for 2 at 20:00", "brief") == "full")
    expect("brief is refused for a ru booking",
           policy.disclosure_level_for("ES", "Забронировать столик", "brief") == "full")
    # The list had no Polish in it at all, while the only canvass ever run was
    # Polish — so the language most likely to ask for `brief` was the one
    # language whose commitments the gate could not see.
    for goal in ("Zarezerwuj stolik na dwie osoby", "Zamów dwie pizze",
                 "Umów wizytę na piątek", "Anuluj rezerwację"):
        expect(f"brief is refused for pl {goal.split()[0].lower()!r}",
               policy.disclosure_level_for("PL", goal, "brief") == "full")
    # …without swallowing the calls brief exists for. A canvass that gets
    # upgraded to `full` on every goal is a gate nobody will leave switched on.
    for goal in ("Czy mają Państwo Ozempic 2 mg?",
                 "Ask if they stock Ozempic 2 mg",
                 "What are your opening hours in August?"):
        expect(f"brief survives a plain question: {goal[:24]!r}",
               policy.disclosure_level_for("PL", goal, "brief") == "brief")


def test_tokens() -> None:
    """Two identities must never be able to share a bearer token.

    Everything per-identity — caps, audit, which tenant is billed — rests on
    this mapping being injective. It used to be a bare dict assignment with a
    comment claiming a collision was impossible to express; the second entry
    simply won.
    """
    print("api tokens")
    expect("distinct tokens map to their identities",
           config._parse_tokens("mac:x,openclaw:y") == {"x": "mac", "y": "openclaw"})
    try:
        config._parse_tokens("mac:same,openclaw:same")
        expect("a shared token is fatal", False)
    except ValueError as e:
        expect("a shared token is fatal", "same token" in str(e))
    try:
        config._parse_tokens("mac")
        expect("a malformed entry is fatal", False)
    except ValueError:
        expect("a malformed entry is fatal", True)


def test_cost() -> None:
    print("cost estimate")
    uk, ua = UK_DID, UA_DID
    expect("US telephony is free, model is not", 0.2 < policy.estimate_usd("US", 300, uk) < 0.3)
    expect("UA is the expensive route",
           policy.estimate_usd("UA", 300, uk) > policy.estimate_usd("ES", 300, uk))
    expect("unmeasured is pessimistic",
           policy.estimate_usd("NP", 300, uk) > policy.estimate_usd("ES", 300, uk))
    expect("zero duration is free", policy.estimate_usd("ES", 0, uk) == 0.0)

    # The ×20–34 origin effect measured in phase 0. Pricing by destination alone
    # under-reported by an order of magnitude exactly when a client overrode the
    # caller ID — the case a spend cap most needs to get right.
    expect("UA caller ID makes an ES call far dearer",
           policy.estimate_usd("ES", 300, ua) > 4 * policy.estimate_usd("ES", 300, uk))
    expect("UA caller ID makes a GB call far dearer",
           policy.estimate_usd("GB", 300, ua) > 4 * policy.estimate_usd("GB", 300, uk))
    expect("UA destinations are caller-ID-independent",
           policy.estimate_usd("UA", 300, ua) == policy.estimate_usd("UA", 300, uk))
    expect("US stays free from either origin",
           policy.estimate_usd("US", 300, ua) == policy.estimate_usd("US", 300, uk))
    expect("an unknown caller ID is priced pessimistically",
           policy.estimate_usd("ES", 300, "+99900000000") > policy.estimate_usd("ES", 300, uk))


def test_state_normalisation() -> None:
    print("agent dialect -> canonical states")
    expect("goal_achieved completes", states.normalise("goal_achieved", None) == ("goal_achieved", "completed"))
    expect("unreachable+voicemail flattens",
           states.normalise("unreachable", "voicemail") == ("voicemail", "voicemail"))
    expect("unreachable+declined completes",
           states.normalise("unreachable", "declined") == ("declined", "completed"))
    expect("no_audio is a failure", states.normalise("no_audio", None) == ("no_audio", "failed"))
    expect("busy stays busy", states.normalise("busy", None) == ("busy", "busy"))
    # An unrecognised disposition means agent/api version skew. Guessing
    # "completed" would hide that AND mark a call successful that may never have
    # connected, so it must degrade to failed.
    expect("unknown degrades to failed", states.normalise("teleported", None) == ("failed", "failed"))
    expect("declined skips extraction", not states.extraction_is_worthwhile("completed", "declined"))
    expect("busy skips extraction", not states.extraction_is_worthwhile("busy", "busy"))
    expect("completed extracts", states.extraction_is_worthwhile("completed", "goal_achieved"))


def test_extractor_helpers() -> None:
    print("extractor helpers")
    schema = {
        "type": "object",
        "properties": {"price": {"type": ["number", "null"]}, "notes": {"type": "string"}},
    }
    strict = extractor._strictify(schema)
    expect("strictify closes the object", strict["additionalProperties"] is False)
    expect("strictify requires every property", strict["required"] == ["price", "notes"])

    envelope = extractor.build_envelope(None)
    expect("envelope tolerates no schema", envelope["properties"]["answers"]["properties"] == {})

    rendered = extractor.render_transcript([
        {"speaker": "assistant", "text": "That is fifteen eu", "t": 12.0, "interrupted": True},
        {"speaker": "user", "text": "fifty", "t": 13.0, "confidence": 0.3},
    ])
    expect("interrupted turns are marked", "INTERRUPTED" in rendered)
    expect("low-confidence callee speech is marked", "LOW CONFIDENCE" in rendered)

    flagged = extractor._merge_unreliable(
        [], {"price": 50, "parking": True}, [{"fact": "price", "value": "15 EUR"}]
    )
    expect("disagreement with a confirmed value is flagged", "price" in flagged)
    agreed = extractor._merge_unreliable(
        [], {"price": 15}, [{"fact": "price", "value": "15 EUR"}]
    )
    expect("agreement is not flagged", agreed == [])
    unconfirmed = extractor._merge_unreliable(
        ["opening_hours"], {"opening_hours": "09:00"}, []
    )
    expect("model-reported unreliability is kept", unconfirmed == ["opening_hours"])

    # Promoting a property to `required` without admitting null would force the
    # model to fill a field it has no evidence for — i.e. to invent a fact about
    # a real phone call.
    optional = extractor._strictify({
        "type": "object",
        "properties": {"notes": {"type": "string"}, "price": {"type": "number"}},
        "required": ["price"],
    })
    expect("a field made required also becomes nullable",
           optional["properties"]["notes"]["type"] == ["string", "null"])
    expect("a genuinely required field is left alone",
           optional["properties"]["price"]["type"] == "number")

    # A callee can say "END TRANSCRIPT" out loud; they cannot guess a nonce.
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    envelope = extractor.build_envelope(schema)
    expect("envelope wraps the caller schema under answers",
           "x" in envelope["properties"]["answers"]["properties"])


def test_mcp_idempotency() -> None:
    print("mcp idempotency")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "mcp"))
    import server  # noqa: PLC0415

    base = {"number": "+34600123456", "goal": "Ask about parking", "language": "es"}
    fp = server._fingerprint({**base, "wait_seconds": 10})

    # The bug this replaced: a retry seconds later must not get a fresh key just
    # because a wall-clock bucket rolled over between the two attempts.
    expect("a repeat reuses the same key",
           server._auto_idempotency_key(fp) == server._auto_idempotency_key(fp))
    expect("wait_seconds does not change the request identity",
           fp == server._fingerprint({**base, "wait_seconds": 30}))
    expect("goal casing and padding do not change identity",
           fp == server._fingerprint({**base, "goal": "  ASK ABOUT PARKING  "}))

    # A corrected schema or caller ID IS a different request — returning the old
    # job would silently ignore the correction.
    expect("a changed schema is a different request",
           fp != server._fingerprint({**base, "answer_schema": {"type": "object"}}))
    expect("a changed caller id is a different request",
           fp != server._fingerprint({**base, "caller_id": "+35799000000"}))
    expect("a changed goal is a different request",
           fp != server._fingerprint({**base, "goal": "Ask about breakfast"}))


# The voice agent's pure-text modules. These import cleanly in a bare
# interpreter — no LiveKit, no realtime SDK — which is the entire reason they are
# separate files. This block used to be thirty lines of fake `sys.modules`
# entries impersonating the whole runtime just to reach a string function; when
# the SDK changed shape the stubs went stale and the tests failed for reasons
# that had nothing to do with the code under test.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "agent"))
import answerer  # noqa: E402
import prompts  # noqa: E402


def test_disclosure_delivered() -> None:
    """The §8 guard: a disclosure only counts if it was actually heard."""
    print("disclosure delivered (BRIEF §8.1)")

    full = ("This is Klava, an AI assistant calling for a potential client. "
            "I'll note down your answer so nothing gets lost.")

    expect("a complete disclosure counts",
           prompts.disclosure_delivered([{"speaker": "assistant", "text": full}], "en"))
    # The phase-1 compliance failure: the callee interrupted and never heard the
    # words identifying an AI, but the stored turn text still contained them.
    expect("an interrupted disclosure does NOT count",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": full, "interrupted": True}], "en"))
    expect("naming the AI without the storage fact does not count",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "This is Klava, an AI assistant calling for a client."}], "en"))
    expect("a callee saying the words does not count",
           not prompts.disclosure_delivered([{"speaker": "user", "text": full}], "en"))
    expect("shredded fragments do not add up to a disclosure",
           not prompts.disclosure_delivered([
               {"speaker": "assistant", "text": "This is Klava, an"},
               {"speaker": "assistant", "text": "for a client. I'll note down your answer"},
           ], "en"))
    # The default and an override both produce a speakable line with the AI and
    # storage facts intact; the override changes only the framing.
    expect("default introduction names a potential client",
           "a potential client" in prompts.disclosure_for("en"))
    custom = prompts.disclosure_for("ru", "light", "вашего постоянного клиента")
    expect("introduce_as override lands in the disclosure",
           "вашего постоянного клиента" in custom and "AI-ассистент" in custom)

    # --- brief level, and Polish (canvassing calls) -------------------------
    #
    # The brief disclosure deliberately carries NO name. If this check still
    # demanded one, disclosure_guard would decide the disclosure never landed and
    # cut across the callee to repeat it — on every canvassing call.
    pl_brief = prompts.disclosure_for("pl", "brief")
    expect("brief PL disclosure has no name in it",
           "klaw" not in pl_brief.lower() and "klav" not in pl_brief.lower())
    expect("brief PL disclosure still says AI and still says it is noted",
           "asystent AI" in pl_brief and "zanotuję" in pl_brief)
    expect("a brief disclosure counts without a name",
           prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": pl_brief}], "pl", "brief"))
    expect("brief is not enough at light level",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": pl_brief}], "pl", "light"))
    expect("a brief disclosure that only greets does not count",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": "Dzień dobry, jedno pytanie."}],
               "pl", "brief"))
    # Polish has no native "v": what the agent says as "Klawa" comes back
    # transcribed either way, and both must satisfy the name check.
    pl_light = prompts.disclosure_for("pl", "light")
    expect("PL light disclosure counts as spelled",
           prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": pl_light}], "pl", "light"))
    expect("PL light disclosure counts transcribed with a v",
           prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": pl_light.replace("Klawa", "Klava")}],
               "pl", "light"))

    # --- the two facts are checked separately -------------------------------
    #
    # `full` says two things `light` does not — that the call is STORED, and that
    # the callee may stop it. Dropping them costs a small share of the words and
    # half of §8, so a whole-sentence similarity score would wave them through.
    en_full = prompts.disclosure_for("en", "full")
    en_light = prompts.disclosure_for("en", "light")
    expect("full counts at full level",
           prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": en_full}], "en", "full"))
    expect("a light disclosure does NOT satisfy a call designated full",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": en_light}], "en", "full"))
    expect("full without the storage clause does not count",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "This is Klava, an AI assistant calling on behalf of a "
                         "potential client. If you would rather I did not, say "
                         "so and I'll end the call."}], "en", "full"))
    expect("full without the opt-out does not count",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "This is Klava, an AI assistant calling on behalf of a "
                         "potential client. This call is transcribed and stored."}],
               "en", "full"))
    expect("a fuller disclosure than asked for still counts",
           prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": en_full}], "en", "light"))

    # --- the disclosure has to be in a language the call is actually in ------
    #
    # The old check looked for AI/storage words from all four languages at once,
    # so at brief — where no name is required — a Polish line closed the guard on
    # a Russian call. It only counts now if a COMPLETE line in that language
    # landed, which is exactly what the prompt's mid-call language switch does.
    ru_brief = prompts.disclosure_for("ru", "brief")
    expect("a Polish disclosure does not satisfy a Russian call",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": pl_brief}], "ru", "brief"))
    expect("a Russian disclosure satisfies a Russian call",
           prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": ru_brief}], "ru", "brief"))
    expect("a switched-language disclosure counts when switching is allowed",
           prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": pl_brief}], "ru", "brief",
               None, ("en", "ru", "es", "pl")))
    expect("switching does not license a stray foreign word",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": "Zanotuję. Так, я AI-ассистент."}],
               "ru", "brief", None, ("en", "ru", "es", "pl")))

    # --- a custom principal is part of the line the guard forces ------------
    custom_en = prompts.disclosure_for("en", "light", "a regular client")
    expect("a custom introduce_as counts when the checker is told about it",
           prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": custom_en}], "en", "light",
               "a regular client"))

    # --- ASR drift, in the safe direction -----------------------------------
    #
    # An 8 kHz line eats endings before it eats stems, and it eats a word here
    # and there. One mangled word must not send the guard across the callee to
    # repeat a disclosure they plainly heard; a missing clause must.
    drifted = ("Dzień dobry, tu Klava, asystent AI dzwoniacy w imieniu "
               "potencjalnego klienta. Zanotuje odpowiedz, zeby nic nie umknelo.")
    expect("a disclosure transcribed without diacritics still counts",
           prompts.disclosure_delivered(
               [{"speaker": "assistant", "text": drifted}], "pl", "light"))
    expect("a disclosure missing its second half does not",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "Dzień dobry, tu Klawa, asystent AI dzwoniący w "
                         "imieniu potencjalnego klienta."}], "pl", "light"))

    # The retention fact, on its own, at every level and in every language.
    #
    # These are regression tests for a measured false positive, not hypotheticals.
    # `brief` is one sentence, so retention is not its own clause, and word-share
    # recall let the storage verb vanish while the line still scored 5 of 6:
    # ru, pl and es all counted a disclosure that told the callee a machine was
    # calling and never told them their answer is kept. That is the half of §8
    # the guard exists to force, and it would never have re-said it.
    dropped = {
        "en": "Hello, this is an AI assistant, the answer.",
        "ru": "Здравствуйте, это AI-ассистент, ответ.",
        "es": "Hola, soy un asistente de IA, la respuesta.",
        "pl": "Dzień dobry, tu asystent AI, odpowiedź.",
    }
    for lang, said in dropped.items():
        expect(f"{lang} brief without the storage verb does not count",
               not prompts.disclosure_delivered(
                   [{"speaker": "assistant", "text": said}], lang, "brief"))

    # Same fact, same hole, one rung up: light's retention half is short too.
    light_dropped = {
        "ru": "Это Клава, AI-ассистент потенциального клиента. "
              "Ответ, чтобы ничего не упустить.",
        "es": "Soy Klava, un asistente de IA que llama de parte de un cliente "
              "potencial. Su respuesta para no perder nada.",
        "pl": "Dzień dobry, tu Klawa, asystent AI dzwoniący w imieniu "
              "potencjalnego klienta. Odpowiedź, żeby nic nie umknęło.",
    }
    for lang, said in light_dropped.items():
        expect(f"{lang} light without the storage verb does not count",
               not prompts.disclosure_delivered(
                   [{"speaker": "assistant", "text": said}], lang, "light"))

    # And the fix must not have bought that by rejecting inflections: the verb
    # comes back off an 8 kHz line in whatever form the speaker used.
    expect("an inflected storage verb still counts",
           prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "Здравствуйте, это AI-ассистент, записываю ответ."}],
               "ru", "brief"))

    # ⚠ The retention stems must not be satisfiable by an unrelated word that
    # happens to be a PREFIX of one. The first version reused `_heard`, which
    # prefix-matches both ways, so the stem "stored" was satisfied by the plain
    # English noun "store" — and the canvass question we ask is literally "do
    # you have Royal Pop in the store right now". The guard proving we said the
    # answer is kept was being passed by the question.
    # Prefix matching was tried twice and was wrong twice, in both directions at
    # once. These four pin the outcome of that: two words that merely BEGIN like
    # a retention verb must not count, and two real inflections that diverge
    # early must.
    expect("'noteworthy' is not 'note'",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "Hello, this is an AI assistant. The answer is "
                         "noteworthy."}], "en", "brief"))
    expect("'guardería' is not 'guardar'",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "Hola, soy un asistente de IA, la respuesta sobre la "
                         "guardería."}], "es", "brief"))
    expect("'anotamos' counts as anotar",
           prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "Hola, soy un asistente de IA, anotamos la respuesta."}],
               "es", "brief"))
    expect("'zanotowano' counts as zanotować",
           prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "Dzień dobry, tu asystent AI, zanotowano odpowiedź."}],
               "pl", "brief"))
    # "do you keep it in stock" must not be able to stand in for "I'll keep it".
    expect("'keep' in the question is not a retention promise",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "Hello, this is an AI assistant, the answer. Do you "
                         "keep Royal Pop in stock?"}], "en", "brief"))

    expect("the noun 'store' does not stand in for 'stored'",
           not prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "Hello, this is an AI assistant, I'll the answer down. "
                         "Do you have Royal Pop in the store?"}], "en", "brief"))
    # The real line plus that same question must still pass, or the fix bought
    # correctness by breaking every canvass call.
    expect("the real line survives the same question",
           prompts.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": prompts.disclosure_for("en", "brief")
                         + " Do you have Royal Pop in the store right now?"}],
               "en", "brief"))

    # The canvass prompt is a separate template, not the long one with sections
    # switched off — and every word of it is paid for on a cold first turn.
    canvass = prompts.build_instructions("Ask if they stock X.", "pl",
                                         disclosure_level="brief")
    conversational = prompts.build_instructions("Ask if they stock X.", "pl")
    expect("brief routes to the short canvass template",
           len(canvass.split()) < len(conversational.split()) / 2)
    expect("the canvass prompt still carries the disclosure verbatim",
           pl_brief in canvass)
    expect("the canvass prompt still requires admitting to being an AI",
           "are an AI, say yes" in canvass)


def test_answerer() -> None:
    """Who picked up, judged from what was said (agent/answerer.py)."""
    print("answerer classification")

    # An IVR that opens with voicemail-ish words is still an IVR: hanging up on
    # it throws away a call that had a human two keypresses away.
    expect("a menu is not voicemail",
           not answerer.looks_like_voicemail(
               "Nie możemy teraz odebrać. Aby połączyć się z apteką, naciśnij 1."))
    expect("real PL voicemail is still caught",
           answerer.looks_like_voicemail("Nagraj wiadomość po sygnale, oddzwonimy."))
    expect("a screener is still not voicemail",
           not answerer.looks_like_voicemail("Please state your name after the tone."))

    # Hold music makes a recogniser invent short confident English fragments.
    # Counting those as the callee talking padded the transcript the extractor
    # reads with turns nobody spoke.
    expect("ASR hallucinations on hold music are noise",
           all(answerer.is_noise_turn(t)
               for t in ("You", "you", "Thank you.", " . ", "Bye")))
    expect("a real short answer is NOT noise",
           not any(answerer.is_noise_turn(t)
                   for t in ("tak", "nie", "Nie ma", "jest")))

    # Substring matching on a phrase list this short is a trap: plain
    # `"press" in text` also matches "pressure", so the very hallucination that
    # started this ("How to take a pressure?") was classified as an IVR menu and
    # bought the caller a minute of patient silence.
    expect("a real menu is a menu",
           answerer.looks_like_menu("Aby połączyć się z apteką, naciśnij 1."))
    expect("press 1 is a menu", answerer.looks_like_menu("To reach reception, press 1"))
    expect("'pressure' is NOT a menu",
           not answerer.looks_like_menu("How to take a pressure?"))
    expect("'depressed' is NOT a menu",
           not answerer.looks_like_menu("she sounded depressed"))


def test_prior_attempt() -> None:
    print("prior attempt note")
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 8, 4, 18, 10, tzinfo=timezone.utc)

    def row(minutes: int, achieved: bool = False, job_id: str = "c_old",
            disposition: str = "callee_hangup"):
        return {"id": job_id,
                "created_at": now - timedelta(minutes=minutes),
                "disposition": disposition,
                "goal_achieved": achieved}

    expect("no history means no line", policy.prior_attempt_note([], now) is None)
    recent = policy.prior_attempt_note([row(8)], now)
    expect("a recent failed call is mentioned", recent is not None)
    expect("it says how long ago", "8 minutes" in (recent or ""))
    expect("one minute is singular",
           "1 minute " in (policy.prior_attempt_note([row(1)], now) or ""))
    # A call that got its answer has no business ringing again, and apologising
    # for one that went fine invites "yes, and I already told you".
    expect("a successful call is not apologised for",
           policy.prior_attempt_note([row(8, achieved=True)], now) is None)
    # Beyond the window nobody remembers the call, and the reminder is worse
    # than silence.
    expect("an old call is past remembering",
           policy.prior_attempt_note([row(300)], now) is None)
    # The job being dispatched is itself in the table by the time this runs.
    expect("the call being placed is not its own prior attempt",
           policy.prior_attempt_note([row(0, job_id="me")], now,
                                     exclude_job_id="me") is None)
    # Clock skew between the API box and Postgres must not produce "about
    # -3 minutes ago".
    expect("a future timestamp is ignored",
           policy.prior_attempt_note([row(-3)], now) is None)
    # A NULL goal_achieved is not evidence of failure — the extractor may simply
    # not have run yet. Decided by the disposition, not guessed. Calls that died
    # on the introduction have nothing to extract and land here, which is the
    # case worth apologising for; a finished call still being read is not.
    expect("null goal_achieved with a failed disposition still counts",
           policy.prior_attempt_note(
               [row(8, achieved=None, disposition="no_audio")], now) is not None)
    expect("null goal_achieved mid-extraction is not apologised for",
           policy.prior_attempt_note(
               [row(8, achieved=None, disposition=None)], now) is None)
    expect("a call still in flight is not apologised for",
           policy.prior_attempt_note(
               [row(2, achieved=None, disposition="in_progress")], now) is None)


if __name__ == "__main__":
    for test in (
        test_destinations, test_caller_id, test_tenant_isolation, test_tenant_config,
        test_disclosure, test_tokens, test_prior_attempt,
        test_cost, test_state_normalisation, test_extractor_helpers,
        test_mcp_idempotency, test_disclosure_delivered, test_answerer,
    ):
        test()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("all checks passed")
