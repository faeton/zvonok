"""The canonical status vocabulary. call-api owns it; everything else is a dialect.

The voice agent has its own words for how a call ended (`goal_achieved`,
`no_audio`, `abandoned`, `callee_hangup`, `setup_failed`, …) and BRIEF §4 names a
different set of terminal call states (`busy`, `no_answer`, `voicemail`, …).
Letting both float was flagged as a phase-2 trap (BRIEF §9, trap 3): a status
that means different things in two services eventually drives a wrong retry, and
a wrong retry redials a stranger. So the agent's vocabulary is treated as an
INPUT DIALECT and normalised here, once, on the way in.
"""

from __future__ import annotations

# Lifecycle (BRIEF §4).
CALL_STATES = (
    "queued", "provisioning", "dialing", "ringing", "in_progress",
    "ending", "post_processing", "completed",
)
TERMINAL_CALL_STATES = frozenset({
    "completed", "busy", "no_answer", "rejected", "voicemail",
    "failed", "canceled", "timed_out", "invalid_number",
})
ALL_CALL_STATES = frozenset(CALL_STATES) | TERMINAL_CALL_STATES

# Separate axis, deliberately (BRIEF §4): a completed call whose extraction
# failed is not a failed call, and must never be redialled to fix it.
PROCESSING_STATES = frozenset({"pending", "extracting", "completed", "failed", "skipped"})

# Deliberately absent: a RETRYABLE_CALL_STATES set. It existed here and nothing
# read it, which implied a retry policy the code does not have — an idempotency
# key always returns its existing job, busy or not, and a redial is expressed by
# a NEW key. Automatic busy/no-answer retries are phase 3; a constant that
# describes them before they exist is worse than no constant.

# What actually happened, from the agent's point of view.
DISPOSITIONS = frozenset({
    "goal_achieved", "callee_hangup", "abandoned", "no_audio", "voicemail",
    "ivr_deadend", "wrong_number", "language_barrier", "declined",
    "setup_failed", "timed_out", "busy", "no_answer", "rejected",
    "invalid_number", "failed", "canceled",
})

# The agent reports `unreachable` plus a reason; we flatten that into a single
# disposition so downstream consumers have one field to switch on.
_UNREACHABLE_REASONS = {
    "voicemail", "ivr_deadend", "wrong_number", "language_barrier", "declined",
}

_CALL_STATUS_BY_DISPOSITION = {
    "goal_achieved": "completed",
    "callee_hangup": "completed",
    "abandoned": "completed",
    "wrong_number": "completed",
    "language_barrier": "completed",
    "declined": "completed",
    "ivr_deadend": "completed",
    "voicemail": "voicemail",
    "no_audio": "failed",
    "setup_failed": "failed",
    "failed": "failed",
    "timed_out": "timed_out",
    "busy": "busy",
    "no_answer": "no_answer",
    "rejected": "rejected",
    "invalid_number": "invalid_number",
    "canceled": "canceled",
}


def normalise(disposition: str | None, unreachable_reason: str | None) -> tuple[str, str]:
    """Agent dialect → (canonical disposition, canonical call_status).

    Unknown input is mapped to `failed` rather than guessed at: a disposition we
    do not recognise is a bug in the agent or a version skew, and inventing
    `completed` for it would hide both.
    """
    if disposition == "unreachable":
        reason = unreachable_reason if unreachable_reason in _UNREACHABLE_REASONS else "ivr_deadend"
        return reason, _CALL_STATUS_BY_DISPOSITION[reason]

    if disposition in _CALL_STATUS_BY_DISPOSITION:
        return disposition, _CALL_STATUS_BY_DISPOSITION[disposition]

    return "failed", "failed"


def extraction_is_worthwhile(call_status: str, disposition: str) -> bool:
    """Should we spend a text-model call on this transcript?

    No, when there is nothing to extract from: the line never carried a
    conversation, or the callee asked us not to keep their words — in which case
    the transcript was discarded and running an extractor over the remains would
    be both pointless and a breach of what the agent promised out loud (§8).
    """
    if disposition == "declined":
        return False
    return call_status in ("completed", "timed_out")
