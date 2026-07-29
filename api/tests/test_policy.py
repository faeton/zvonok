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

from zvonok_api import extractor, policy, states  # noqa: E402

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


def test_disclosure() -> None:
    print("disclosure level")
    expect("info call in ES is light", policy.disclosure_level_for("ES", "Ask about parking") == "light")
    expect("any call in DE is full", policy.disclosure_level_for("DE", "Ask about parking") == "full")
    expect("booking is full", policy.disclosure_level_for("ES", "Book a table for 2 at 20:00") == "full")
    expect("ru booking is full", policy.disclosure_level_for("ES", "Забронировать столик") == "full")
    expect("explicit override wins", policy.disclosure_level_for("DE", "Book a table", "light") == "light")


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


def test_disclosure_delivered() -> None:
    """The §8 guard: a disclosure only counts if it was actually heard.

    Imported with the LiveKit stack stubbed out — agent.py pulls in the whole
    realtime runtime at module scope, and this is pure text logic. The stubs are
    only touched at import time.
    """
    print("disclosure delivered (BRIEF §8.1)")
    import types  # noqa: PLC0415

    for name in (
        "livekit", "livekit.api", "livekit.rtc", "livekit.agents",
        "livekit.plugins", "livekit.plugins.xai",
        "openai", "openai.types", "openai.types.beta",
        "openai.types.beta.realtime", "openai.types.beta.realtime.session",
        "report",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["livekit"].api = sys.modules["livekit.api"]
    sys.modules["livekit"].rtc = sys.modules["livekit.rtc"]
    sys.modules["livekit.plugins"].xai = sys.modules["livekit.plugins.xai"]
    sys.modules["openai.types.beta.realtime.session"].TurnDetection = object
    for attr in ("Agent", "AgentSession", "JobContext", "RunContext", "WorkerOptions",
                 "cli", "get_job_context"):
        setattr(sys.modules["livekit.agents"], attr, type(attr, (), {}))
    # @function_tool() is applied at class-definition time, so it has to be a
    # real decorator factory rather than a bare sentinel.
    sys.modules["livekit.agents"].function_tool = lambda *a, **k: (lambda fn: fn)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "agent"))
    import agent as voice  # noqa: PLC0415

    full = ("This is Klava, an AI assistant calling for a potential client. "
            "I'll note down your answer so nothing gets lost.")

    expect("a complete disclosure counts",
           voice.disclosure_delivered([{"speaker": "assistant", "text": full}], "en"))
    # The phase-1 compliance failure: the callee interrupted and never heard the
    # words identifying an AI, but the stored turn text still contained them.
    expect("an interrupted disclosure does NOT count",
           not voice.disclosure_delivered(
               [{"speaker": "assistant", "text": full, "interrupted": True}], "en"))
    expect("naming the AI without the storage fact does not count",
           not voice.disclosure_delivered(
               [{"speaker": "assistant",
                 "text": "This is Klava, an AI assistant calling for a client."}], "en"))
    expect("a callee saying the words does not count",
           not voice.disclosure_delivered([{"speaker": "user", "text": full}], "en"))
    expect("shredded fragments do not add up to a disclosure",
           not voice.disclosure_delivered([
               {"speaker": "assistant", "text": "This is Klava, an"},
               {"speaker": "assistant", "text": "for a client. I'll note down your answer"},
           ], "en"))
    # The default and an override both produce a speakable line with the AI and
    # storage facts intact; the override changes only the framing.
    expect("default introduction names a potential client",
           "a potential client" in voice.disclosure_for("en"))
    custom = voice.disclosure_for("ru", "light", "вашего постоянного клиента")
    expect("introduce_as override lands in the disclosure",
           "вашего постоянного клиента" in custom and "AI-ассистент" in custom)


if __name__ == "__main__":
    for test in (
        test_destinations, test_caller_id, test_disclosure, test_cost,
        test_state_normalisation, test_extractor_helpers, test_mcp_idempotency,
        test_disclosure_delivered,
    ):
        test()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("all checks passed")
