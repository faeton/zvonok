"""Request/response shapes (BRIEF §5.4)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CallRequest(BaseModel):
    number: str = Field(..., description="Destination in E.164, e.g. +34911234567")
    goal: str = Field(..., min_length=8, max_length=2000)
    language: Literal["ru", "en", "es", "pl"]

    caller_id: str | None = None
    answer_schema: dict[str, Any] | None = None
    # Lower bound matters: a negative or tiny value propagates into protobuf
    # durations and an asyncio.sleep that expires immediately, turning a request
    # bug into a dial that hangs up on someone mid-greeting.
    max_duration_seconds: int | None = Field(None, ge=30, le=600)
    # Only the profile the worker actually implements. Accepting the others
    # would record a job as `nova-sonic` and then place a Grok Voice call — an
    # audit trail that lies. Widen this when the agent can honour them (§5.3).
    profile: Literal["grok-voice"] = "grok-voice"
    # Omitted → chosen from the destination country and the goal (BRIEF §8.1).
    # An explicit value always wins, so a caller who knows their jurisdiction
    # is never overridden by our heuristic.
    #
    # "brief" is never chosen automatically: it is the shortest disclosure we
    # have, and shortening the disclosure must be a decision the requesting
    # agent makes for a call it knows is a one-question canvass — not something
    # a heuristic does on its own.
    disclosure_level: Literal["brief", "light", "full"] | None = None
    # Who the agent says it is calling for: "your regular customer", "Ивана".
    # Omitted → a per-language default ("a potential client"). Free text chosen
    # by the requesting agent from the task's context; Russian must arrive
    # already in the genitive because it is spliced into the spoken disclosure.
    introduce_as: str | None = Field(None, min_length=2, max_length=120)
    # Proper nouns to bias speech recognition towards: a drug, a brand, a part
    # number. These are the words an 8 kHz line destroys first and the words the
    # whole call usually turns on. Bounded — a long list degrades recognition
    # instead of helping it.
    keywords: list[str] | None = Field(None, max_length=16)
    callback_url: str | None = None
    idempotency_key: str | None = Field(None, max_length=200)
    # Bounded synchronous wait. This catches instant SIP failures (busy, invalid
    # number) so an agent does not have to poll to learn the number was wrong.
    # It is NOT "wait for the call to finish" — see BRIEF §9 phase-2 trap 7.
    wait_seconds: int = Field(0, ge=0, le=30)

    @field_validator("callback_url")
    @classmethod
    def _https_only(cls, v: str | None) -> str | None:
        if v and not v.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("callback_url must be https (or loopback)")
        return v


class CallCreated(BaseModel):
    call_id: str
    call_status: str
    processing_status: str
    number: str
    country: str | None = None
    caller_id: str | None = None
    disclosure_level: str
    est_cost_usd_at_cap: float | None = None
    # True when an existing job was returned instead of a new dial being placed.
    deduplicated: bool = False
    # Present only when wait_seconds caught a terminal outcome before returning.
    disposition: str | None = None
    sip_status: str | None = None


class Turn(BaseModel):
    speaker: str
    text: str
    t: float | None = None
    # A turn cut short by barge-in contains words the callee may never have
    # heard. Surfaced rather than silently dropped (BRIEF §9 phase-2 trap 9).
    interrupted: bool = False
    confidence: float | None = None


class CallStatus(BaseModel):
    call_id: str
    call_status: str
    processing_status: str
    disposition: str | None = None
    unreachable_reason: str | None = None
    sip_status: str | None = None
    number: str
    country: str | None = None
    caller_id: str | None = None
    language: str
    goal: str
    disclosure_level: str
    duration_seconds: float | None = None
    est_cost_usd: float | None = None
    created_at: str
    answered_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    summary: str | None = None


class CallResult(BaseModel):
    call_id: str
    call_status: str
    processing_status: str
    disposition: str | None = None
    duration_seconds: float | None = None
    answers: dict[str, Any] | None = None
    summary: str | None = None
    goal_achieved: bool | None = None
    # What the agent read back to the callee and got agreement on. Kept apart
    # from `answers` so the two can be compared — where they disagree, the value
    # is not trustworthy (BRIEF §9 phase-2 trap 8).
    captured: list[dict[str, Any]] | None = None
    unreliable_fields: list[str] | None = None
    est_cost_usd: float | None = None
    extraction_error: str | None = None
    transcript_url: str


class TranscriptResponse(BaseModel):
    call_id: str
    call_status: str
    disposition: str | None = None
    redacted: bool = False
    turns: list[Turn]


# --- internal (agent → call-api) -------------------------------------------


class AgentEvent(BaseModel):
    """Progress ping from the worker. Advisory: the final report is what counts."""

    event: Literal["dispatched", "dialing", "ringing", "answered", "ending"]
    room_name: str | None = None
    detail: dict[str, Any] | None = None


class AgentFinal(BaseModel):
    """The agent's end-of-call report — the same body it writes to disk.

    Delivery is at-least-once and keyed by job_id, so this endpoint must be
    idempotent: a retry after a timed-out POST is normal, not an error.
    """

    job_id: str
    call_status: str | None = None
    disposition: str | None = None
    unreachable_reason: str | None = None
    sip_status: str | None = None
    duration_seconds: float | None = None
    # Audit: which framing the agent actually spoke on this call.
    introduce_as: str | None = None
    redacted: bool = False
    turns: list[Turn] = Field(default_factory=list)
    captured: list[dict[str, Any]] = Field(default_factory=list)
