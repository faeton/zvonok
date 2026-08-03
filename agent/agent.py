"""zvonok voice agent — one goal-directed outbound PSTN call per job.

Dispatched with metadata:

    {"number": "+34...", "goal": "...", "language": "en",
     "caller_id": "+44...", "max_duration_seconds": 300, "job_id": "..."}

This module is the CALL LIFECYCLE and nothing else. What the agent says lives in
`prompts.py`, how it decides who answered in `answerer.py`, every clock in
`timing.py`, and the realtime model in `voice.py` — all four are importable in a
bare interpreter, which is the point: they are the parts that change after a call
goes wrong, and they should be testable without the LiveKit runtime.

Everything the callee says is UNTRUSTED (BRIEF §6). This agent therefore has only
call-control tools and no access to any of our systems. Structured answer
extraction is a separate text-model pass over the finished transcript, run by
call-api — deliberately not done here.

Design note on termination: every call must end exactly once, and every call must
leave a transcript. Both are load-bearing for money (a call left open bills per
second) and for call-api's state machine (a lie about disposition drives a wrong
retry). Termination is therefore funnelled through Terminator, and the shutdown
callback is registered BEFORE dialling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    get_job_context,
)

import report
from answerer import is_noise_turn, looks_like_menu, looks_like_voicemail
from prompts import (
    DISCLOSURE_LEVELS,
    PROBE,
    build_instructions,
    disclosure_delivered,
    disclosure_for,
)
from timing import (
    DEFAULT_MAX_DURATION,
    END_OF_TURN_SILENCE_MS,
    HARD_MAX_DURATION,
    MAX_SILENCE_NUDGES,
    NO_RESPONSE_BUDGET_SECONDS,
    NO_SPEECH_BUDGET_SECONDS,
    OPENING_SILENCE_SECONDS,
    QUEUE_PATIENCE_SECONDS,
    QUEUE_PATIENCE_TOTAL,
    RINGING_TIMEOUT_SECONDS,
    SILENCE_NUDGE_SECONDS,
    output_gain,
)
from voice import apply_gain, build_realtime_model

logger = logging.getLogger("zvonok")
logger.setLevel(logging.INFO)
# No basicConfig: livekit-agents installs its own root handler, and adding a
# second one makes every line appear twice.

OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID", "")
TRANSCRIPT_DIR = Path(os.getenv("ZVONOK_TRANSCRIPT_DIR", "./transcripts"))
OUTPUT_GAIN = output_gain()

# The tenant routing key: one worker per billing account, and call-api decides
# whose account a call is placed on purely by dispatching to a name.
#
# UNSET means the single-tenant default. SET BUT EMPTY must not, and the
# difference is worth a guard rather than a default. docker-compose passes an
# unconfigured variable through as an empty string, so a second worker whose name
# was forgotten would come up nameless — and a nameless LiveKit worker accepts
# ANY job, including the other tenant's, and would place their call on this
# container's Zadarma trunk with this container's caller ID. That failure is
# completely silent: the call connects and sounds perfectly normal.
AGENT_NAME = os.getenv("ZVONOK_AGENT_NAME", "zvonok-caller").strip()
if not AGENT_NAME:
    raise SystemExit(
        "ZVONOK_AGENT_NAME is set but empty. A worker with no name accepts every "
        "tenant's jobs and would place them on this trunk, billed to this "
        "account. Set it, or unset it entirely for a single-tenant deployment."
    )

# Same unset-vs-empty distinction. UNSET is the manual path (dispatch by hand, no
# trunk configured yet); SET BUT EMPTY is compose passing through a variable the
# operator forgot, and a worker that accepts jobs it cannot dial burns a
# concurrency slot and a daily-call allowance per attempt.
if "SIP_OUTBOUND_TRUNK_ID" in os.environ and not OUTBOUND_TRUNK_ID.strip():
    raise SystemExit(
        "SIP_OUTBOUND_TRUNK_ID is set but empty. This worker would accept jobs "
        "and fail every dial after admission has already counted them."
    )

# Defence in depth against the API handing us a caller ID that is not ours.
# call-api validates `caller_id` against the requesting tenant's verified list
# before dispatch, but this process is the one that actually dials, and it is the
# only place that knows which Zadarma account this trunk belongs to. A DID is
# verified per SIP account, so presenting another account's number is a call
# billed to the wrong balance with a callback pointing at the wrong person.
# Optional: unset means "trust the API", which is the single-tenant behaviour.
OWNED_CALLER_IDS = frozenset(
    x.strip() for x in os.getenv("ZVONOK_OWNED_CALLER_IDS", "").split(",") if x.strip()
)

# Which billing account this worker dials for. Checked against the tenant in the
# dispatch metadata before anything is dialled.
#
# Until this existed, placement isolation was ENTIRELY `agent_name`: call-api
# dispatches to a name, LiveKit hands the job to whichever process registered
# that name, and that process dials on whatever SIP_OUTBOUND_TRUNK_ID its own env
# happens to hold. Nothing ever asserted the two agreed. Three ways to break it,
# all silent and all ending in a normal-sounding call billed to the wrong
# Zadarma account: worker env drift; a name registered at LiveKit that differs
# from the one config checked (require() validates configuration, not who
# actually registered); and an identity whose ZVONOK_TENANT_<IDENTITY> is simply
# unset, which resolves to the default tenant while every carefully set _FRIEND
# variable sits unused.
#
# The tenant NAME is safe to put in metadata — it is not a credential, and
# agent_name already effectively discloses it. The trunk id deliberately still
# does not travel that way (dispatch metadata is persisted in Redis and logged).
TENANT = os.getenv("ZVONOK_TENANT", "default").strip() or "default"

DEFAULT_GOAL = "Confirm you have reached the right person, ask how their day is going, and thank them."

# Hard cap on simultaneous paid calls (BRIEF §6). Enforced at worker admission;
# call-api MUST also enforce it before dispatch, since a second worker would not
# know about this one.
MAX_CONCURRENT_CALLS = int(os.getenv("ZVONOK_MAX_CONCURRENT_CALLS", "2"))


# The agent's disposition vocabulary mapped onto BRIEF §4 terminal call states.
# Writing "completed" for anything that merely reached the shutdown callback
# would feed call-api's state machine lies: a timed-out call, or one that
# answered with no media, is not a completed call and must not drive the same
# retry decision.
_DISPOSITION_TO_CALL_STATUS = {
    "goal_achieved": "completed",
    "completed": "completed",
    "callee_hangup": "completed",
    "abandoned": "completed",
    "unreachable": "completed",  # we did reach a line — refined by reason below
    "no_audio": "failed",
    "setup_failed": "failed",
    "timed_out": "timed_out",
    # Dial failures: the disposition IS the terminal call state (BRIEF §4).
    "busy": "busy",
    "no_answer": "no_answer",
    "rejected": "rejected",
    "invalid_number": "invalid_number",
    "failed": "failed",
}


def call_status_for(disposition: str, unreachable_reason: str | None) -> str:
    if disposition == "unreachable" and unreachable_reason == "voicemail":
        return "voicemail"
    return _DISPOSITION_TO_CALL_STATUS.get(disposition, "completed")


class Terminator:
    """Single owner of "this call is over".

    Without this, end_call, mark_unreachable, the duration guard, the silence
    watchdog and the callee-hangup handler all raced: each set a disposition and
    each deleted the room, so the recorded outcome depended on which fired last
    (end_call setting goal_achieved, then the duration guard overwriting it with
    timed_out mid-farewell). First terminal cause wins; the rest are ignored.
    """

    def __init__(self) -> None:
        self.disposition: str | None = None
        self.unreachable_reason: str | None = None
        self.done = asyncio.Event()
        self._claimed = False

    def claim(self, disposition: str, reason: str | None = None) -> bool:
        """Return True if this caller is the one that gets to end the call."""
        if self._claimed:
            logger.info(
                "termination already claimed as %s; ignoring %s",
                self.disposition, disposition,
            )
            return False
        self._claimed = True
        self.disposition = disposition
        self.unreachable_reason = reason
        return True

    async def hangup(self) -> None:
        """Delete the room, which drops the SIP leg.

        Bounded retry, and — importantly — a failure is NOT assumed to mean the
        room is already gone. Treating every error as "already closed" meant a
        transient control-plane blip silently disarmed the hard duration cap and
        left a billing conversation open indefinitely.
        """
        ctx = get_job_context()
        for attempt in range(1, 4):
            try:
                await ctx.api.room.delete_room(
                    api.DeleteRoomRequest(room=ctx.room.name)
                )
                self.done.set()
                return
            except api.TwirpError as e:
                if getattr(e, "code", "") in ("not_found", "NotFound"):
                    logger.info("room already gone")
                    self.done.set()
                    return
                logger.warning(
                    "delete_room failed (%s) attempt %d/3: %s", e.code, attempt, e
                )
            except Exception as e:  # noqa: BLE001 — teardown must not raise
                logger.warning("delete_room error attempt %d/3: %s", attempt, e)
            await asyncio.sleep(1.0 * attempt)

        # Last line of defence is the server-side max_call_duration set on the
        # SIP participant, which does not depend on this process at all.
        logger.error(
            "could not delete room after 3 attempts — falling back to the "
            "server-side max_call_duration fuse"
        )
        self.done.set()


class ZvonokCaller(Agent):
    def __init__(
        self,
        *,
        goal: str,
        language: str,
        dial_info: dict[str, Any],
        term: Terminator,
        disclosure_level: str = "light",
        answer_schema: dict[str, Any] | None = None,
        introduce_as: str | None = None,
        turns: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            instructions=build_instructions(
                goal,
                language,
                disclosure_level=disclosure_level,
                answer_schema=answer_schema,
                introduce_as=introduce_as,
            )
        )
        self.dial_info = dial_info
        self.term = term
        self.language = language
        self.disclosure_level = disclosure_level
        self.introduce_as = introduce_as
        # Live view of the conversation so far, shared with the entrypoint. The
        # decline gate below needs to know what was actually said.
        self.turns = turns if turns is not None else []
        self.participant: rtc.RemoteParticipant | None = None
        # Facts the agent claims to have confirmed out loud. call-api's extractor
        # re-derives answers from the transcript independently; these exist so it
        # can tell "the callee said 17:00" from "17:00 was read back and agreed".
        self.captured: list[dict[str, str]] = []
        self._asked_to_confirm = False
        self._decline_challenged = False

    def set_participant(self, participant: rtc.RemoteParticipant) -> None:
        self.participant = participant

    async def realtime_audio_output_node(
        self, audio: Any, model_settings: Any
    ) -> Any:  # noqa: ANN401 — mirrors the loosely-typed base signature
        # Callees kept saying the agent was audibly quieter than a human caller.
        # Boost the realtime model's PCM here, before it is encoded to G.711 —
        # the only point in the path we control the level at all.
        async for frame in Agent.default.realtime_audio_output_node(
            self, audio, model_settings
        ):
            yield apply_gain(frame, OUTPUT_GAIN)

    def hangup_after_goodbye(self, session: AgentSession) -> None:
        """Hang up once the agent has finished its closing line.

        Must NOT await the speech from inside the tool that triggered it: the
        speech handle waits on the tool to return while the tool waits on
        playout, and livekit-agents raises on that circular wait. So detach a
        task — by the time it runs, the tool has returned and the farewell can
        play out in full. Bounded, because a model that never stops talking must
        not hold a paid PSTN line open.
        """

        async def _run() -> None:
            try:
                # Give the post-tool farewell a moment to be created, or we would
                # observe "idle" in the gap before it starts.
                await asyncio.sleep(0.5)
                await asyncio.wait_for(session.wait_for_idle(), timeout=20)
            except asyncio.TimeoutError:
                logger.warning("goodbye did not finish within 20s — hanging up anyway")
            except Exception as e:  # noqa: BLE001 — session may already be closing
                logger.info("wait_for_idle ended early: %s", e)
            await self.term.hangup()

        _spawn(_run(), "hangup_after_goodbye")

    @function_tool()
    async def end_call(self, ctx: RunContext) -> str:
        """Use when the goal is achieved, or clearly cannot be achieved, and you
        have thanked the person."""
        # Structural confirmation gate. Telling the model in the prompt to repeat
        # numbers back is not reliable — on a real call it accepted a spoken time
        # without confirming it, because a conversational model optimises for
        # moving the conversation along, not for auditability. So refuse the
        # first end_call when nothing has been confirmed. Bounded to one retry: a
        # hard block would strand calls that legitimately have nothing to capture
        # (wrong number, refusal, unreachable).
        if not self.captured and not self._asked_to_confirm:
            self._asked_to_confirm = True
            logger.info("end_call refused once — nothing confirmed yet")
            return (
                "Not yet. First read the key value back to them out loud — the "
                "time, price, number or date — and get their agreement. Then "
                "call record_answer with it, then call end_call again. If there "
                "is genuinely nothing to capture, just call end_call again."
            )

        if not self.term.claim("goal_achieved"):
            return "call is already ending"
        logger.info("end_call requested by agent")
        self.hangup_after_goodbye(ctx.session)
        return "call is ending; say a brief goodbye now"

    @function_tool()
    async def record_answer(self, ctx: RunContext, fact: str, value: str) -> str:
        """Record a fact you have obtained AND read back to the person for
        confirmation. Call this for every number, time, price or date.

        Args:
            fact: What this is, e.g. "padel_time", "price_per_night", "parking".
            value: Exactly what was confirmed, e.g. "17:00", "15 EUR", "yes".
        """
        self.captured.append({"fact": fact, "value": value})
        # The NAME only, never the value. If the callee later objects to being
        # transcribed we discard the turns and the captured facts (§8) — but we
        # cannot reach into Docker's log files to unsay a price or a person's
        # name that was written there minutes earlier.
        logger.info("recorded a value for %s", fact)
        return f"recorded {fact}={value}"

    @function_tool()
    async def send_dtmf(self, ctx: RunContext, digits: str) -> str:
        """Press keys on the phone keypad, e.g. to navigate a menu.

        Args:
            digits: The digits to press, e.g. "1" or "302". Only 0-9, * and #.
        """
        allowed = set("0123456789*#")
        digits = "".join(d for d in digits if d in allowed)
        if not digits:
            return "no valid digits to send"

        logger.info("sending DTMF: %s", digits)
        room = get_job_context().room
        for d in digits:
            code = {"*": 10, "#": 11}.get(d, ord(d) - ord("0"))
            await room.local_participant.publish_dtmf(code=code, digit=d)
            await asyncio.sleep(0.15)  # tone gap, or the far end merges the digits
        return f"pressed {digits}"

    @function_tool()
    async def mark_unreachable(self, ctx: RunContext, reason: str) -> str:
        """Use when the goal cannot be pursued at all on this call.

        Args:
            reason: One of voicemail, ivr_deadend, wrong_number, language_barrier, declined.
        """
        valid = {"voicemail", "ivr_deadend", "wrong_number", "language_barrier", "declined"}
        reason = reason if reason in valid else "ivr_deadend"

        # `declined` is the one irreversible action on this call: it discards the
        # transcript (§8), which also destroys the evidence of whether it was
        # warranted. It therefore needs more than the model's say-so.
        #
        # Learned on a live call: the callee said "шо" — one Russian syllable
        # meaning roughly "what?" — on an English call, and the model read that
        # as a refusal to be recorded, discarded everything and hung up without
        # explaining itself. A confused noise is not consent withdrawal.
        #
        # The gate is principled rather than a word list: YOU CANNOT DECLINE
        # SOMETHING YOU WERE NEVER TOLD ABOUT. If the disclosure has not actually
        # been delivered — in one uninterrupted turn — then whatever the callee
        # just made a sound about, it was not our retention of their answer.
        #
        # Bounded to a single challenge, like the end_call gate: a person who
        # genuinely objects must never be trapped on the line arguing with a
        # machine, so a second call goes through regardless.
        if (
            reason == "declined"
            and not self._decline_challenged
            and not disclosure_delivered(self.turns, self.language, self.disclosure_level)
        ):
            self._decline_challenged = True
            logger.info("decline refused once — disclosure was never delivered")
            return (
                "Not yet — they have not actually heard what you are doing, so "
                "that was not a refusal. Most likely they did not catch what you "
                "said. Say your introduction again, once, clearly and briefly, "
                "and ask if that is alright. If they then object in any way, "
                "call this again with reason 'declined' and it will be honoured "
                "immediately."
            )

        if not self.term.claim("unreachable", reason):
            return "call is already ending"
        logger.info("marked unreachable: %s", reason)
        self.hangup_after_goodbye(ctx.session)
        return f"marked unreachable: {reason}; end the call politely now"


async def say_verbatim(
    session: AgentSession, text: str, *, allow_interruptions: bool = True
) -> None:
    """Speak a fixed line as close to word-for-word as this model allows.

    NOT session.say(): Grok Voice is a pure speech-to-speech model and reports
    `supports_say=False`, so say() raises unless a TTS is also attached. Adding a
    TTS purely for these two lines would put a second, different-sounding voice
    on the call. So the fixed text goes through generate_reply with a verbatim
    instruction instead.

    `allow_interruptions=False` is the part that actually matters for the
    disclosure: it is the one utterance on the call that a barge-in must not be
    able to truncate.
    """
    await session.generate_reply(
        instructions=(
            f'Say exactly this, word for word, and then stop. Add nothing '
            f'before or after it — no greeting, no filler, no offer to help:\n'
            f'"{text}"'
        ),
        allow_interruptions=allow_interruptions,
    )


def _spawn(coro: Any, name: str) -> asyncio.Task:
    """Create a background task that reports its own failures.

    A bare asyncio.create_task drops both the reference and the exception: when
    the silence watchdog died on a closing session, its protection vanished for
    the rest of the call and nothing said so.
    """
    task = asyncio.create_task(coro, name=name)

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("background task %s died: %r", name, exc)

    task.add_done_callback(_done)
    return task


async def entrypoint(ctx: JobContext) -> None:
    meta: dict[str, Any] = json.loads(ctx.job.metadata or "{}")

    number = meta.get("number")
    if not number:
        raise ValueError("dispatch metadata must include 'number'")

    # Refuse before dialling, not after. A job that reached the wrong worker is
    # about to leave on the wrong account's trunk, presenting the wrong account's
    # caller ID, billed to the wrong balance — and it will sound completely
    # normal to everyone involved, which is why nothing downstream would ever
    # catch it. Loud and free is the only acceptable outcome here: nothing has
    # been dialled, so nothing has been spent.
    #
    # Absent means an older call-api that does not send it; trust it, exactly as
    # an unset OWNED_CALLER_IDS means "trust the API".
    job_tenant = (meta.get("tenant") or "").strip()
    if job_tenant and job_tenant != TENANT:
        logger.error(
            "job %s is tenant %r but this worker dials for %r — refusing. "
            "Check ZVONOK_AGENT_NAME/ZVONOK_TENANT wiring: a duplicate or "
            "drifted agent_name sends calls to the wrong account's trunk.",
            meta.get("job_id") or ctx.job.id, job_tenant, TENANT,
        )
        raise ValueError(
            f"job belongs to tenant {job_tenant!r}, this worker is {TENANT!r}"
        )

    goal = meta.get("goal") or DEFAULT_GOAL
    language = meta.get("language", "en")
    caller_id = meta.get("caller_id")
    job_id = meta.get("job_id") or ctx.job.id
    # "light" by default (§8): the storage fact in plain words. call-api picks
    # this from a policy table; here it is simply passed through.
    disclosure_level = meta.get("disclosure_level", "light")
    if disclosure_level not in DISCLOSURE_LEVELS:
        logger.warning("unknown disclosure_level %r — using light", disclosure_level)
        disclosure_level = "light"
    # Who to say the call is for ("your regular customer", "Ивана"). Free text
    # chosen by the requesting agent; for Russian it must already be in the
    # genitive. Bounded because it is spliced into both the system prompt and the
    # verbatim disclosure line.
    introduce_as = meta.get("introduce_as")
    if introduce_as is not None:
        introduce_as = str(introduce_as).strip()[:120] or None
    # Proper nouns to bias the recogniser towards — a drug name, a brand, a part
    # number. Bounded because they are passed straight to the ASR: xAI allows 100
    # terms of 50 characters, and we stay well inside that because a long list
    # degrades recognition rather than helping it.
    keywords = meta.get("keywords")
    if not isinstance(keywords, list):
        keywords = []
    keywords = [str(k).strip()[:40] for k in keywords if str(k).strip()][:16]

    # Drives what the agent ASKS as well as what the extractor reads out of the
    # transcript afterwards (BRIEF §9 phase-2 trap 8).
    answer_schema = meta.get("answer_schema")
    if answer_schema is not None and not isinstance(answer_schema, dict):
        logger.warning("answer_schema is not an object — ignoring")
        answer_schema = None
    max_duration = min(
        int(meta.get("max_duration_seconds", DEFAULT_MAX_DURATION)), HARD_MAX_DURATION
    )

    logger.info(
        "job %s: dialing %s (lang=%s, caller_id=%s, max=%ss)",
        job_id, number, language, caller_id, max_duration,
    )

    await ctx.connect()

    term = Terminator()
    # Declared before the agent so the two share one list: the agent's decline
    # gate reads what has actually been said so far, and the entrypoint appends
    # to it as the conversation happens.
    turns: list[dict[str, Any]] = []
    agent = ZvonokCaller(
        goal=goal,
        language=language,
        dial_info=meta,
        term=term,
        disclosure_level=disclosure_level,
        answer_schema=answer_schema,
        introduce_as=introduce_as,
        turns=turns,
    )

    session = AgentSession(llm=build_realtime_model(language, keywords))

    # Live transcript capture. We collect as we go rather than only dumping
    # session.history at the end, so a mid-call crash still leaves evidence.
    t_zero = time.monotonic()
    callee_spoke = False
    last_activity = time.monotonic()
    # When a menu has told us to hold, silence stops meaning "abandoned" and
    # starts meaning "queued". Without this the watchdog nudged into hold music
    # and then hung up on a switchboard that was about to connect a human.
    # Bounded rather than open-ended: the wait is patience, not a blank cheque.
    queued_until = 0.0
    # When the current queue wait began, for the total-patience ceiling.
    queued_since = 0.0
    # When the callee last stopped talking, for the reply-gap measurement below.
    callee_stopped_at: float | None = None
    # Has anything a recogniser called WORDS come from the far end? Distinct from
    # `callee_spoke`, which is VAD energy and is therefore satisfied by hold
    # music, a fax tone or a noisy line. Only this justifies waiting.
    heard_speech = False
    answered_at = 0.0
    voicemail_hangup = False
    tasks: list[asyncio.Task] = []
    written = False

    def build_body(sip_status: str | None = None) -> dict[str, Any]:
        """The end-of-call report. One dict, two destinations.

        The file on disk and the POST to call-api carry exactly the same object
        by construction — which is what lets call-api's janitor recover a lost
        callback from the shared volume without a second parser to keep in sync.
        """
        disposition = term.disposition or "completed"
        reason = term.unreachable_reason

        # A callee who objects to being transcribed was told, out loud, that
        # nothing would be kept. Honour that literally: keep only the minimal
        # audit record (§8), never their words.
        declined = disposition == "unreachable" and reason == "declined"
        body: dict[str, Any] = {
            "job_id": job_id,
            "number": number,
            "caller_id": caller_id,
            "language": language,
            "profile": "grok-voice",
            "disclosure_level": disclosure_level,
            "introduce_as": introduce_as,
            "call_status": call_status_for(disposition, reason),
            "disposition": disposition,
            "unreachable_reason": reason,
            "sip_status": sip_status,
            "duration_seconds": round(time.monotonic() - t_zero, 1),
            "ended_at": datetime.now(timezone.utc).isoformat(),
        }
        if declined:
            body["redacted"] = True
            body["redaction_reason"] = "callee declined transcription; content discarded"
            body["turns"] = []
            body["captured"] = []
        else:
            body["goal"] = goal
            body["captured"] = agent.captured
            body["turns"] = turns
        return body

    async def finalize(sip_status: str | None = None) -> None:
        """Persist the call exactly once: to disk first, then to call-api.

        Disk first is deliberate. The HTTP POST is the fast path, but it is also
        the one that can hang or arrive at a call-api that is restarting; the
        file is written to a volume call-api can read, so a lost callback
        degrades to a delayed reconciliation instead of a call that appears never
        to have happened (BRIEF §9 phase-2 trap 4).

        Exactly once, because the shutdown callback is registered before dialling
        (so a failure between there and the conversation still leaves a record),
        which means the dial-failure path and the callback would both fire.
        """
        nonlocal written
        if written:
            return
        written = True

        body = build_body(sip_status)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = TRANSCRIPT_DIR / f"{stamp}-{job_id}.json"
        payload = json.dumps(body, ensure_ascii=False, indent=2)

        # Never let a disk problem lose the call: log the transcript before
        # trying to persist it, and don't raise out of a shutdown callback.
        try:
            TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(payload)
            logger.info(
                "transcript written: %s (%s, %d turns%s)",
                path, body["call_status"], len(body["turns"]),
                ", redacted" if body.get("redacted") else "",
            )
        except OSError:
            logger.exception("could not write %s — dumping inline instead", path)
            logger.error("TRANSCRIPT %s: %s", job_id, payload)

        await report.post_final(body)

    # Registered BEFORE dialling. If anything between here and the conversation
    # throws — the realtime session failing to initialise is the realistic case —
    # the PSTN leg has already been answered and is billing, so we must still
    # tear down and still leave a record.
    async def on_shutdown() -> None:
        for t in tasks:
            t.cancel()
        # Hang up if nothing else already has. An exception raised after the
        # callee picked up (the opening probe failing, say) would otherwise end
        # the job while leaving the answered, billing SIP leg in the room, and
        # only the server-side max_call_duration fuse would eventually drop it —
        # up to five minutes of paid silence.
        if term.claim("setup_failed"):
            logger.warning("shutting down with the call still up — hanging up")
            await term.hangup()
        await finalize()

    ctx.add_shutdown_callback(on_shutdown)

    # Also registered before dialling: a participant that answers and drops again
    # while we are still awaiting session start would otherwise disconnect before
    # the listener exists, and events are not replayed to late subscribers.
    @ctx.room.on("participant_disconnected")
    def _on_disconnected(p: rtc.RemoteParticipant) -> None:
        if p.identity != number:
            return
        logger.info("callee hung up")
        if term.claim("callee_hangup"):
            _spawn(term.hangup(), "hangup_on_callee_disconnect")

    # VAD, not transcription. `conversation_item_added` only fires once speech has
    # been transcribed — a second or more after the fact — far too late to decide
    # whether to open the conversation. This fires on audio energy.
    @session.on("user_state_changed")
    def _on_user_state(ev: Any) -> None:  # noqa: ANN401
        nonlocal callee_spoke, last_activity, callee_stopped_at
        state = getattr(ev, "new_state", None)
        if state == "speaking":
            callee_spoke = True
            last_activity = time.monotonic()
        elif state == "listening":
            # They stopped talking. From here the clock the CALLEE experiences is
            # running, and it is the only latency number that matters.
            callee_stopped_at = time.monotonic()

    # THE measurement. Every latency claim about this agent so far — including
    # "the model takes nine seconds" — was inferred from transcript timestamps,
    # which are stamped when a turn is TRANSCRIBED, not when it was heard or
    # spoken. That is not evidence, and it sent one diagnosis off in the wrong
    # direction. This logs the real gap: callee stops talking → our audio starts.
    @session.on("agent_state_changed")
    def _on_agent_state(ev: Any) -> None:  # noqa: ANN401
        nonlocal callee_stopped_at
        if getattr(ev, "new_state", None) != "speaking":
            return
        if callee_stopped_at is None:
            return
        gap = time.monotonic() - callee_stopped_at
        callee_stopped_at = None
        # ⚠ Read this number for what it is, and no more. It is measured between
        # two LOCAL events, and it therefore:
        #   - EXCLUDES our own END_OF_TURN_SILENCE_MS. `listening` is emitted
        #     after the server VAD has already reported speech-stopped, so that
        #     wait has elapsed before this clock starts. The callee's experienced
        #     pause is roughly this plus END_OF_TURN_SILENCE_MS.
        #   - ENDS when the agent starts producing audio locally, not when the
        #     first sample reaches the handset. SIP framing, the carrier and the
        #     PSTN leg are all still ahead of it.
        # So it is a lower bound on what the person heard, useful for spotting a
        # regression and for comparing against ttft — not a full accounting.
        logger.info(
            "reply gap %.2fs local (add ~%.2fs end-of-turn wait + egress)",
            gap, END_OF_TURN_SILENCE_MS / 1000,
        )

    @session.on("metrics_collected")
    def _on_metrics(ev: Any) -> None:  # noqa: ANN401 — shape varies by version
        m = getattr(ev, "metrics", ev)
        ttft = getattr(m, "ttft", None)
        if ttft is not None and ttft >= 0:
            logger.info("model ttft %.2fs", ttft)

    @session.on("conversation_item_added")
    def _on_item(ev: Any) -> None:  # noqa: ANN401 — event shape varies by version
        nonlocal last_activity, voicemail_hangup, queued_until, heard_speech, queued_since
        item = getattr(ev, "item", ev)
        text = getattr(item, "text_content", None) or ""
        if not text:
            return
        speaker = getattr(item, "role", "unknown")

        # Drop ASR hallucinations from the transcript the extractor reads. See
        # answerer.is_noise_turn for why this does NOT bound a queue.
        if speaker == "user" and is_noise_turn(text):
            logger.debug("ignoring noise turn: %r", text)
            return

        if speaker == "user":
            # Someone said something a recogniser was willing to call words.
            # This — not VAD energy — is the evidence that a human is on the
            # line, and it is what the watchdog uses to decide whether waiting
            # is still justified.
            heard_speech = True

        last_activity = time.monotonic()
        turns.append({
            "speaker": speaker,
            "text": text,
            "t": round(time.monotonic() - t_zero, 2),
            # An assistant turn cut off by barge-in contains words that were
            # never played to the callee. Recording the flag is what stops the
            # extractor from later "confirming" a price the agent only started
            # to read back (BRIEF §9 phase-2 trap 9).
            "interrupted": bool(getattr(item, "interrupted", False)),
            # Present on transcribed user speech; lets the extractor discount a
            # digit it half-heard rather than treating it as stated fact.
            "confidence": getattr(item, "transcript_confidence", None),
        })

        # A menu was heard: the next stretch of quiet is a queue, not a dead
        # line. Refreshed on every menu turn, so a switchboard that repeats its
        # announcement keeps the grace alive rather than exhausting it — bounded
        # by QUEUE_PATIENCE_TOTAL in the watchdog.
        if speaker == "user" and looks_like_menu(text):
            if not queued_since:
                queued_since = time.monotonic()
            queued_until = time.monotonic() + QUEUE_PATIENCE_SECONDS
            logger.info("menu or hold detected — waiting quietly for up to %.0fs",
                        QUEUE_PATIENCE_SECONDS)
        elif speaker == "user" and queued_since:
            # Someone who is not a recording is on the line: the queue is over.
            # Leaving the state set would keep the watchdog in its silent branch
            # while an actual human waited for us to say something.
            logger.info("human on the line after %.0fs of queue — leaving the queue",
                        time.monotonic() - queued_since)
            queued_since = 0.0
            queued_until = 0.0

        # Safety net: the prompt tells the model to hang up on voicemail, and on
        # a real answering machine it did not — it introduced itself to a
        # recording instead. Enforce in code rather than paying to be talked at.
        if speaker == "user" and not voicemail_hangup and looks_like_voicemail(text):
            voicemail_hangup = True
            logger.warning("voicemail detected in code — hanging up: %.80s", text)
            if term.claim("unreachable", "voicemail"):
                _spawn(term.hangup(), "hangup_on_voicemail")

    # Start the session BEFORE dialing, so nothing the callee says on pickup is
    # missed. No room_input_options: RoomInputOptions is deprecated in 1.6, and
    # the only thing we would set there — noise_cancellation — is Cloud-only.
    session_started = _spawn(
        session.start(agent=agent, room=ctx.room), "session_start"
    )
    tasks.append(session_started)

    dial_request = api.CreateSIPParticipantRequest(
        room_name=ctx.room.name,
        sip_trunk_id=OUTBOUND_TRUNK_ID,
        sip_call_to=number,
        participant_identity=number,
        wait_until_answered=True,  # blocks until pickup, or raises on failure
    )
    # Caller ID per destination country is a COST lever, not just an answer-rate
    # one (BRIEF §9 phase-0: x20–34 price swing). Falls back to the trunk default.
    if caller_id:
        # Second check, after call-api's. Dropping to the trunk default rather
        # than failing the call: the number is wrong, not the request, and this
        # trunk's own default is by definition a DID of the right account.
        if OWNED_CALLER_IDS and caller_id not in OWNED_CALLER_IDS:
            logger.error(
                "job %s: refusing caller_id %s — not a DID of this worker's "
                "account (%s). Falling back to the trunk default. This means "
                "call-api routed a job here that belongs to another tenant.",
                job_id, caller_id, ",".join(sorted(OWNED_CALLER_IDS)),
            )
            caller_id = None
        else:
            dial_request.sip_number = caller_id
    # Server-side fuses, independent of this process staying alive or the room
    # delete succeeding. Without these, a crashed agent leaves a billing call up.
    dial_request.max_call_duration.FromTimedelta(timedelta(seconds=max_duration + 30))
    dial_request.ringing_timeout.FromTimedelta(timedelta(seconds=RINGING_TIMEOUT_SECONDS))

    try:
        await report.post_event(job_id, "dialing", number=number, caller_id=caller_id)
        await ctx.api.sip.create_sip_participant(dial_request)
    except api.TwirpError as e:
        sip_code = e.metadata.get("sip_status_code")
        sip_status = e.metadata.get("sip_status")
        logger.error("dial failed: %s (SIP %s %s)", e.message, sip_code, sip_status)
        # Map the carrier's verdict onto our terminal states (BRIEF §4).
        term.claim({
            "486": "busy", "600": "busy",
            "408": "no_answer", "480": "no_answer",
            "603": "rejected", "403": "rejected",
            "404": "invalid_number",
        }.get(str(sip_code), "failed"))
        # Persist here rather than leaving it to the shutdown callback, so the
        # SIP detail — the carrier's actual verdict, and the only evidence of
        # why this number failed — is not lost.
        await finalize(sip_status=f"{sip_code} {sip_status}")
        ctx.shutdown()
        return

    # From here the line is answered and billing. Everything is wrapped so that
    # no failure path can leave it up.
    try:
        answered_at = time.monotonic()
        await session_started
        participant = await asyncio.wait_for(
            ctx.wait_for_participant(identity=number), timeout=20
        )
        logger.info("answered: %s", participant.identity)
        agent.set_participant(participant)
        await report.post_event(job_id, "answered")
    except Exception as e:  # noqa: BLE001
        logger.exception("setup failed after answer — tearing down: %r", e)
        if term.claim("setup_failed"):
            await term.hangup()
        ctx.shutdown()
        return

    # Hard duration cap. Runs regardless of what the model decides to do.
    async def duration_guard() -> None:
        await asyncio.sleep(max_duration)
        logger.warning("max_duration %ss reached — ending call", max_duration)
        if term.claim("timed_out"):
            await term.hangup()

    tasks.append(_spawn(duration_guard(), "duration_guard"))

    # Don't speak first. A real person says "Hello?" and waits; talking over that
    # is the tell of a robocall. Whoever speaks first also tells the model which
    # branch of the prompt applies (person / screener / voicemail / IVR).
    #
    # If they DO speak, the realtime model's server-side VAD already generates a
    # reply on its own — calling generate_reply as well would talk over it.
    deadline = time.monotonic() + OPENING_SILENCE_SECONDS
    while time.monotonic() < deadline:
        if callee_spoke or session.user_state == "speaking" or any(
            t["speaker"] == "user" for t in turns
        ):
            logger.info("callee spoke first — letting the model take the lead")
            break
        await asyncio.sleep(0.1)
    else:
        # Probe, do NOT introduce. Playing the full introduction here silently
        # hard-codes the "a person answered" branch before anything has been
        # heard — which is exactly how a real answering machine got a polite
        # introduction instead of a hangup. Voicemail and PBX systems commonly
        # insert half a second to three seconds of silence before their greeting,
        # so silence at this point is not evidence of a human.
        logger.info("silence after pickup — probing, not introducing")
        await say_verbatim(session, PROBE.get(language, PROBE["en"]))

    # Someone picked up and then went quiet — common when a person is distracted,
    # or when a screener has handed over and the human is waiting for us. Nudge
    # rather than sit in dead air, but bounded.
    async def silence_watchdog() -> None:
        nonlocal last_activity
        nudges = 0
        while not term.done.is_set():
            await asyncio.sleep(0.5)

            # Held in a queue: the silence is the system working as intended.
            # Neither nudge nor hang up — talking to hold music achieves nothing,
            # and hanging up throws away a call that was connecting.
            #
            # The queue is an EXCLUSIVE phase, and that is the point. While we
            # are in it nothing else may end the call, because everything else
            # would end it wrongly: the nudge cycle would talk into hold music,
            # and the budgets below would report `no_audio` or `abandoned` for a
            # switchboard that was working exactly as designed — a wrong
            # disposition drives a wrong retry.
            #
            # It ends in exactly three ways, and two of them are here:
            #   - a human speaks         → cleared in _on_item, we fall through
            #   - TOTAL patience elapsed → the switchboard is looping its
            #     announcement and refreshing `queued_until` forever
            #   - the queue goes quiet   → no menu, no music, no human for
            #     QUEUE_PATIENCE_SECONDS; most likely the line dropped
            # Both of the latter are `ivr_deadend`: we reached a machine and
            # never got past it, which is a different thing from a dead line.
            if queued_since:
                now = time.monotonic()
                waited = now - queued_since
                if waited > QUEUE_PATIENCE_TOTAL:
                    logger.warning(
                        "queued %.0fs with no human — giving up (ivr_deadend)", waited
                    )
                elif now > queued_until:
                    logger.warning(
                        "queue went quiet %.0fs ago and no human arrived — giving "
                        "up (ivr_deadend)", now - queued_until + QUEUE_PATIENCE_SECONDS,
                    )
                else:
                    last_activity = now
                    continue
                if term.claim("unreachable", "ivr_deadend"):
                    await term.hangup()
                return

            # Nothing but noise has ever come from the far end. VAD alone does
            # not count: music and line noise trip it, and treating that as "a
            # person is there" is what let a queue run to the duration cap.
            if not heard_speech and time.monotonic() - answered_at > NO_SPEECH_BUDGET_SECONDS:
                logger.warning(
                    "no intelligible speech in %.0fs — ending call",
                    NO_SPEECH_BUDGET_SECONDS,
                )
                if term.claim("no_audio"):
                    await term.hangup()
                return

            # Dead line: nothing has ever been heard from the far end. Cut it
            # without finishing the nudge cycle — there is nobody to nudge.
            if not callee_spoke and time.monotonic() - answered_at > NO_RESPONSE_BUDGET_SECONDS:
                logger.warning(
                    "no sound from callee in %.0fs — ending call (no_audio)",
                    NO_RESPONSE_BUDGET_SECONDS,
                )
                if term.claim("no_audio"):
                    await term.hangup()
                return

            if time.monotonic() - last_activity < SILENCE_NUDGE_SECONDS:
                continue
            # Only when it is genuinely our move: not mid-sentence, not while
            # they are talking, not while the model is still thinking.
            if session.agent_state != "listening" or session.user_state == "speaking":
                continue

            if nudges >= MAX_SILENCE_NUDGES:
                # A carrier can return 200 OK with no media at all. Without this
                # the call sat open for the full max_duration, burning a paid
                # PSTN leg and realtime-model minutes on pure silence.
                disposition = "no_audio" if not callee_spoke else "abandoned"
                logger.warning(
                    "no response after %d nudges — ending call (%s)",
                    nudges, disposition,
                )
                if term.claim(disposition):
                    await term.hangup()
                return

            nudges += 1
            last_activity = time.monotonic()
            logger.info("silence nudge %d/%d", nudges, MAX_SILENCE_NUDGES)
            try:
                await session.generate_reply(
                    instructions=(
                        "The line has gone quiet. In one short sentence, in the "
                        "language you are already speaking, gently check whether "
                        "they are still there and repeat your question briefly."
                    )
                )
            except Exception as e:  # noqa: BLE001 — session may be closing
                logger.info("nudge failed (%r) — watchdog continues", e)

    tasks.append(_spawn(silence_watchdog(), "silence_watchdog"))

    async def disclosure_guard() -> None:
        """Guarantee the §8 disclosure is actually heard, in one piece.

        On a real call the model's introduction was shredded by barge-in — the
        callee interrupted twice and never once heard "Klava, an AI assistant".
        That is a compliance failure, not a quality nit, so it cannot depend on
        the model winning a race against an impatient human and line noise.

        Waits for a genuine two-way exchange before firing, so that the
        four-words-only rule for call screeners (§5.3.1) is not violated: a
        screener produces one utterance and hands over, a person keeps talking.
        """
        while not term.done.is_set():
            await asyncio.sleep(0.5)
            if term.disposition:  # call already ending
                return
            user_turns = sum(1 for t in turns if t["speaker"] == "user")
            agent_turns = sum(1 for t in turns if t["speaker"] == "assistant")
            if user_turns < 2 or agent_turns < 1:
                continue
            if disclosure_delivered(turns, language, disclosure_level):
                return
            # Wait for our move so we don't cut across either party.
            if session.agent_state != "listening" or session.user_state == "speaking":
                continue
            text = disclosure_for(language, disclosure_level, introduce_as)
            logger.warning("disclosure never completed — forcing it: %s", text)
            for attempt in (1, 2):
                try:
                    # Uninterruptible: the one utterance on this call that a
                    # barge-in must not be able to truncate.
                    await say_verbatim(session, text, allow_interruptions=False)
                except Exception as e:  # noqa: BLE001 — session may be closing
                    logger.warning("could not force disclosure: %r", e)
                    return
                # Verify against what was actually transcribed, not against our
                # intention — the same standard the guard applies to the model.
                #
                # Poll rather than sleep once: transcription of our own speech
                # lags playout by more than a second, and a fixed 1 s wait
                # declared a perfectly good disclosure "not complete" and made
                # the agent say it a second time.
                for _ in range(12):  # up to ~6 s
                    await asyncio.sleep(0.5)
                    if disclosure_delivered(turns, language, disclosure_level):
                        logger.info("forced disclosure delivered (attempt %d)", attempt)
                        return
                logger.warning("forced disclosure still not complete (attempt %d)", attempt)
            return

    tasks.append(_spawn(disclosure_guard(), "disclosure_guard"))
    # Deliberately no wait here: the job outlives this function, and the shutdown
    # callback registered above is what persists the transcript.


if __name__ == "__main__":

    def concurrency_load(worker: Any) -> float:  # noqa: ANN401
        """Cap simultaneous paid calls (BRIEF §6).

        LiveKit's default admission control is CPU-based, which has nothing to do
        with how many phone lines we are paying for — this box would happily
        accept a dozen concurrent calls. Returning a value at or above
        load_threshold makes the worker stop accepting jobs.
        """
        active = len(getattr(worker, "active_jobs", ()))
        if active >= MAX_CONCURRENT_CALLS:
            logger.warning("at concurrency cap (%d active) — refusing new jobs", active)
            return 1.0
        return 0.0

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=AGENT_NAME,
            load_fnc=concurrency_load,
            load_threshold=1.0,
            # The worker's health/debug HTTP server defaults to 0.0.0.0:8081.
            # de1 is a shared host and matomo already owns 8081 — and since this
            # container uses host networking, that collision is fatal at startup.
            host="127.0.0.1",
            port=int(os.getenv("ZVONOK_AGENT_PORT", "18130")),
        )
    )
