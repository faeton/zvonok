#!/usr/bin/env python3
"""zvonok MCP server — gives an agent a telephone.

A thin adapter over call-api (BRIEF §5.5). It holds no state and makes no
decisions: policy, caps and idempotency all live in call-api, because a second
client must not be able to bypass them by talking to a different adapter.

Run over stdio (Claude Code). OpenClaw can use either this or plain REST — it
runs on de1, so for it call-api is already on localhost.

    ZVONOK_API_URL=http://<call-api-host>:8130 \\
    ZVONOK_API_TOKEN=... \\
    uv run --with 'mcp<2' --with httpx python server.py

(mcp is pinned below 2.0: the 2.x line dropped `mcp.server.fastmcp.FastMCP`.)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.environ.get("ZVONOK_API_URL", "http://127.0.0.1:8130").rstrip("/")
API_TOKEN = os.environ.get("ZVONOK_API_TOKEN", "")

mcp = FastMCP("zvonok")


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=API_URL,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        timeout=60,
    )


# request fingerprint -> (key, issued_at). Sliding, in-process.
_KEY_MEMO: dict[str, tuple[str, float]] = {}
_KEY_TTL_SECONDS = 1800


def _auto_idempotency_key(fingerprint: str) -> str:
    """A stable key for a repeated request, remembered rather than computed.

    An LLM client re-issues a tool call after a timeout, a reconnect, or simply
    because it decided to try again — and each of those would otherwise be a
    second real phone call to a real person.

    The obvious implementation, hashing the request together with a wall-clock
    bucket, has a hole exactly where it matters: a request at 12:09:59 whose
    response is lost and is retried at 12:10:01 lands in a different bucket, so
    the retry gets a fresh key and dials again. The failure is most likely
    precisely when the boundary is nearest, because that is when the client is
    still waiting.

    Remembering the key instead makes the window slide with the request. Within
    this process a repeat always reuses the original key, with no boundary to
    fall across. Across a restart the key changes — but a restart within seconds
    of a lost response is the case call-api's same-number-in-flight check
    already refuses, so the two cover each other.

    Pass an explicit `idempotency_key` to control this yourself.
    """
    now = time.time()
    for stale in [k for k, (_, t) in _KEY_MEMO.items() if now - t > _KEY_TTL_SECONDS]:
        del _KEY_MEMO[stale]

    hit = _KEY_MEMO.get(fingerprint)
    if hit is not None:
        return hit[0]

    key = f"auto-{hashlib.sha256(f'{fingerprint}|{now}'.encode()).hexdigest()[:24]}"
    _KEY_MEMO[fingerprint] = (key, now)
    return key


def _fingerprint(payload: dict[str, Any]) -> str:
    """Everything that shapes the call.

    Not just number/goal/language: a caller who corrects the answer_schema or
    the caller ID and re-issues is making a DIFFERENT request, and returning the
    original job would silently ignore the correction and hand back a result in
    the wrong shape.
    """
    shape = {
        k: v for k, v in payload.items()
        if k not in ("wait_seconds", "idempotency_key")
    }
    if isinstance(shape.get("goal"), str):
        shape["goal"] = shape["goal"].strip().lower()
    return json.dumps(shape, sort_keys=True, ensure_ascii=False)


@mcp.tool()
def phone_call(
    number: str,
    goal: str,
    language: str = "en",
    answer_schema: dict[str, Any] | None = None,
    caller_id: str | None = None,
    max_duration_seconds: int = 300,
    wait_seconds: int = 10,
    disclosure_level: str | None = None,
    introduce_as: str | None = None,
    keywords: list[str] | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Place a real phone call to accomplish a task, and get the answer back.

    This calls an actual human being on the public telephone network and costs
    real money. Use it for things that genuinely require a phone: asking a hotel
    about parking, booking a table, checking opening hours. One task, one call.
    Never use it for anything unsolicited or repetitive.

    THE CALL IS NOT FINISHED WHEN THIS RETURNS. A phone call takes 30 seconds to
    several minutes. This returns as soon as the number is ringing or the callee
    picks up, so you learn immediately if the number was busy or invalid. To get
    the answer, call `phone_call_result(call_id)` afterwards — typically 60-180
    seconds later. Do not block the user waiting; tell them the call is under way.

    Args:
        number: Destination in E.164, e.g. "+34911234567". Country must be on
            the allowlist; premium-rate and satellite ranges are refused.
        goal: What to accomplish, written for the assistant rather than to be
            read aloud, e.g. "Find out whether guests can park onsite and what
            it costs per night."
        language: "en", "ru", "es" or "pl". The call opens in this language.
        answer_schema: Optional JSON Schema for the facts you want back. Give
            each property a `description` — it tells the assistant what to ask
            about on the call, not just what to extract afterwards. Use nullable
            types (e.g. {"type": ["number","null"]}) for anything that might not
            be obtainable.
        caller_id: Optional override; must be a number verified on the account.
            Leave unset — the default is chosen per destination country and is a
            large cost factor.
        max_duration_seconds: Hard cap on the call, default 300, maximum 600.
        wait_seconds: How long to wait for a dialling verdict before returning
            (0-30). This never waits for the conversation to finish.
        disclosure_level: "brief", "light" or "full". Leave unset for anything
            that is a conversation — it is then chosen from the destination
            country and the goal, and "full" adds explicit
            transcribed-and-stored wording plus an offer to end the call.
            Set "brief" DELIBERATELY, and only for a one-question canvassing
            call — ringing round pharmacies for a drug, shops for a part, clinics
            for a slot. It drops the name and the who-we-are-calling-for clause,
            opens with "this is an AI assistant, I'll note the answer down" and
            goes straight to the question, and it tells the assistant to take the
            first answer and hang up. Pair it with a short max_duration_seconds
            (90-120): a canvass that turns into a conversation has already
            failed. It is refused for goals that book or commit something.
        introduce_as: Who the assistant says it is calling for, e.g. "a potential
            client" (the default), "your regular customer", or the owner's first
            name when the callee personally knows them. Pick it from the task's
            context: a business being offered custom responds best to "client"
            framing; a personal contact should hear the actual name. In Russian
            supply the genitive ("вашего постоянного клиента", "Ивана"). The
            owner's name/number are never spoken in the introduction either way —
            only later, if the booking needs them.
        keywords: Proper nouns the call turns on — a drug, a brand, a part
            number, a surname. These bias speech recognition towards words an
            8 kHz phone line destroys first, and they are the single cheapest
            thing you can do for accuracy on a call about a named thing. Give
            the spellings a native speaker would use ("Ozempic", "semaglutyd").
            Keep it short and specific; a long list makes recognition worse, not
            better, so up to about a dozen.
        idempotency_key: Set this if you want to control de-duplication. By
            default identical requests within ten minutes collapse onto one call
            so a retry cannot dial the same person twice.
    """
    payload: dict[str, Any] = {
        "number": number,
        "goal": goal,
        "language": language,
        "max_duration_seconds": max_duration_seconds,
        "wait_seconds": max(0, min(wait_seconds, 30)),
    }
    if answer_schema:
        payload["answer_schema"] = answer_schema
    if caller_id:
        payload["caller_id"] = caller_id
    if disclosure_level:
        payload["disclosure_level"] = disclosure_level
    if introduce_as:
        payload["introduce_as"] = introduce_as
    if keywords:
        payload["keywords"] = keywords

    payload["idempotency_key"] = idempotency_key or _auto_idempotency_key(
        _fingerprint(payload)
    )

    with _client() as client:
        resp = client.post("/v1/calls", json=payload)

    if resp.status_code >= 400:
        return json.dumps(
            {
                "error": _detail(resp),
                "status_code": resp.status_code,
                "note": "No call was placed. Fix the request, do not retry blindly.",
            },
            ensure_ascii=False,
            indent=2,
        )

    body = resp.json()
    # Deliberately not shaped like a result. A client that sees answer-shaped
    # keys will report them to the user as the outcome of the call, and at this
    # point there is no outcome (BRIEF §9 phase-2 trap 7).
    out = {
        "call_id": body["call_id"],
        "call_status": body["call_status"],
        "calling": body["number"],
        "from": body.get("caller_id"),
        "disclosure_level": body.get("disclosure_level"),
    }
    if body.get("deduplicated"):
        out["deduplicated"] = True
        out["note"] = (
            "This request matched a call already placed — no second call was "
            "made. Poll phone_call_result(call_id) for its outcome."
        )
    elif body["call_status"] in _TERMINAL:
        out["disposition"] = body.get("disposition")
        out["sip_status"] = body.get("sip_status")
        out["note"] = "The call ended before a conversation started. See disposition."
    else:
        out["note"] = (
            "The call is in progress and has NOT produced an answer yet. Call "
            "phone_call_result(call_id) in about 60-180 seconds. Tell the user "
            "the call is under way rather than waiting silently."
        )
    return json.dumps(out, ensure_ascii=False, indent=2)


_TERMINAL = {
    "completed", "busy", "no_answer", "rejected", "voicemail",
    "failed", "canceled", "timed_out", "invalid_number",
}


@mcp.tool()
def phone_call_result(call_id: str, include_transcript: bool = False) -> str:
    """Get the outcome of a call placed with `phone_call`.

    If `processing_status` is not yet "completed" or "skipped", the call or its
    analysis is still running — wait ~30 seconds and ask again.

    Read `unreliable_fields` before trusting a number. Phone lines are 8 kHz and
    digits get misheard; a field listed there was heard once and never read back
    for confirmation, so treat it as a lead rather than a fact.

    Args:
        call_id: The id returned by `phone_call`.
        include_transcript: Also return the turn-by-turn transcript. Turns marked
            `interrupted` were cut off and may never have been heard by the
            other person.
    """
    with _client() as client:
        resp = client.get(f"/v1/calls/{call_id}/result")
        if resp.status_code >= 400:
            return json.dumps({"error": _detail(resp)}, ensure_ascii=False, indent=2)
        body = resp.json()

        if include_transcript:
            tr = client.get(f"/v1/calls/{call_id}/transcript")
            if tr.status_code < 400:
                doc = tr.json()
                body["transcript"] = doc["turns"]
                if doc.get("redacted"):
                    body["transcript_note"] = (
                        "The callee declined to be transcribed, so their words "
                        "were discarded as promised. Only an audit record remains."
                    )

    if body.get("processing_status") in ("pending", "extracting"):
        body["note"] = "Still processing — ask again in about 30 seconds."
    elif body.get("processing_status") == "failed":
        # Without this, the obvious next move for a client staring at a call
        # with no answers is to place the call again — spending money on the
        # phone network to fix a text-model problem, and calling a stranger a
        # second time for nothing.
        body["note"] = (
            "The CALL succeeded but reading the answers out of it failed. Do NOT "
            "place the call again — the transcript is already here. Use "
            "phone_call_reextract(call_id), or read the transcript yourself with "
            "include_transcript=true."
        )
    return json.dumps(body, ensure_ascii=False, indent=2)


@mcp.tool()
def phone_call_reextract(call_id: str) -> str:
    """Re-read the answers out of a call that already happened.

    Use this when `phone_call_result` reports `processing_status: "failed"`, or
    when you want the same conversation re-read against a corrected schema. It
    costs a text-model call and nothing on the phone network — nobody is dialled
    and nobody is disturbed a second time.

    Args:
        call_id: The id of a call that has already finished.
    """
    with _client() as client:
        resp = client.post(f"/v1/calls/{call_id}/reextract")
    if resp.status_code >= 400:
        return json.dumps({"error": _detail(resp)}, ensure_ascii=False, indent=2)
    return json.dumps(resp.json(), ensure_ascii=False, indent=2)


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", resp.text))[:1000]
    except ValueError:
        return resp.text[:1000]


if __name__ == "__main__":
    if not API_TOKEN:
        raise SystemExit("ZVONOK_API_TOKEN is not set")
    mcp.run()
