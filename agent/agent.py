"""zvonok phase-1 voice agent.

One goal-directed outbound PSTN call per job. Dispatched with metadata:

    {"number": "+34...", "goal": "...", "language": "en",
     "caller_id": "+44...", "max_duration_seconds": 300, "job_id": "..."}

Everything the callee says is UNTRUSTED (BRIEF §6). This agent therefore has only
call-control tools and no access to any of our systems. Structured answer
extraction is a separate text-model pass over the finished transcript, run by
call-api in phase 2 — deliberately not done here.

Design note on termination: every call must end exactly once, and every call
must leave a transcript. Both are load-bearing for money (a call left open bills
per second) and for the phase-2 state machine (a lie about disposition drives a
wrong retry). Termination is therefore funnelled through Terminator, and the
shutdown callback is registered BEFORE dialling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
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
from livekit.plugins import xai
from openai.types.beta.realtime.session import TurnDetection

import report

logger = logging.getLogger("zvonok")
logger.setLevel(logging.INFO)
# No basicConfig: livekit-agents installs its own root handler, and adding a
# second one makes every line appear twice.

OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID", "")
TRANSCRIPT_DIR = Path(os.getenv("ZVONOK_TRANSCRIPT_DIR", "./transcripts"))

# Phase-1 defaults. Phase 2 moves these into call-api's request validation.
DEFAULT_GOAL = "Confirm you have reached the right person, ask how their day is going, and thank them."
DEFAULT_MAX_DURATION = 300
HARD_MAX_DURATION = 600  # BRIEF §6 cap — never exceeded regardless of metadata
RINGING_TIMEOUT_SECONDS = 45

# Hard cap on simultaneous paid calls (BRIEF §6). Enforced at worker admission;
# phase 2's call-api must ALSO enforce it before dispatch, since a second worker
# would not know about this one.
MAX_CONCURRENT_CALLS = int(os.getenv("ZVONOK_MAX_CONCURRENT_CALLS", "2"))

# Three different silences, three different timers — conflating them is what made
# the agent both jumpy and slow at the same time.
#
# 1. Nobody said anything after pickup → we probe (NOT introduce, see below).
#    3.0, not 1.5: a real callee's "hello" has to cross the PSTN leg, the SIP
#    media path and VAD before we see it. At 1.5 s we repeatedly decided the line
#    was silent while the person had in fact already spoken, and talked over them.
OPENING_SILENCE_SECONDS = 3.0
# 2. They said something and stopped → how long to wait before assuming the turn
#    is over. Must be generous: a screener says "Hello?" and only THEN "please
#    state your name after the tone". Answering the bare "Hello?" means missing
#    the actual request. (OpenAI's server_vad default is 500 ms — too eager.)
END_OF_TURN_SILENCE_MS = 800
# 3. Mid-call dead air after someone has already spoken → nudge, don't hang.
SILENCE_NUDGE_SECONDS = 6.0
MAX_SILENCE_NUDGES = 2
# 4. Absolute budget for a line where the callee has NEVER made a sound, measured
#    from answer. A person who is merely distracted trips timer 3 and gets the
#    full nudge cycle; a line that answered with no media has nothing to wait for.
NO_RESPONSE_BUDGET_SECONDS = 20.0
# How eagerly the callee's audio counts as speech (and therefore as a barge-in).
# Raised from the 0.5 default because PSTN line noise was interrupting the agent
# mid-sentence, producing "speech not done in time after interruption" storms.
BARGE_IN_THRESHOLD = 0.65

# Linear gain on the agent's outgoing speech, applied to the realtime model's
# PCM before it hits the G.711 leg. Callees consistently reported the agent as
# quiet-but-intelligible; 1.4 is about +3 dB — a clearly audible step up without
# flirting with clipping on a model that already peaks conservatively. Samples
# are clamped to int16, so an over-eager env override degrades to distortion,
# not to a crash.


def _output_gain() -> float:
    """Parse ZVONOK_OUTPUT_GAIN defensively.

    A typo in one env var must not take the whole calling capability down at
    import time, and 0/NaN would silently mute every call — worse than loud.
    Clamped to [0.1, 4.0]: beyond ~4× everything clips into noise anyway.
    """
    raw = os.getenv("ZVONOK_OUTPUT_GAIN", "1.4")
    try:
        gain = float(raw)
    except ValueError:
        logger.warning("ZVONOK_OUTPUT_GAIN=%r is not a number — using 1.4", raw)
        return 1.4
    if not math.isfinite(gain) or gain <= 0:
        logger.warning("ZVONOK_OUTPUT_GAIN=%r is not usable — using 1.4", raw)
        return 1.4
    return min(max(gain, 0.1), 4.0)


OUTPUT_GAIN = _output_gain()


def apply_gain(frame: rtc.AudioFrame, gain: float) -> rtc.AudioFrame:
    if gain == 1.0:
        return frame
    samples = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) * gain
    boosted = np.clip(samples, -32768, 32767).astype(np.int16)
    return rtc.AudioFrame(
        data=boosted.tobytes(),
        sample_rate=frame.sample_rate,
        num_channels=frame.num_channels,
        samples_per_channel=frame.samples_per_channel,
    )

# Identity, per language. Names are spelled for PRONUNCIATION, not for
# paperwork: a Cyrillic "Иван" is read correctly by the model on a Russian
# call, the Latin spelling is not.
LANGUAGE_NAMES = {"en": "English", "ru": "Russian", "es": "Spanish"}

# The owner's personal details come EXCLUSIVELY from the environment (they are
# configured on the deploy host, never in this repository — the same rule as
# secrets). Unset values simply drop out of the prompt: the agent then has no
# name or number to give and says the client will confirm instead.
IDENTITY = {
    "en": {
        "assistant": os.getenv("ZVONOK_ASSISTANT_NAME", "Klava"),
        "owner": os.getenv("ZVONOK_OWNER_NAME", ""),
        "owner_full": os.getenv("ZVONOK_OWNER_FULLNAME", ""),
        "role": "AI assistant",
    },
    "ru": {
        "assistant": os.getenv("ZVONOK_ASSISTANT_NAME_RU", "Клава"),
        "owner": os.getenv("ZVONOK_OWNER_NAME_RU", os.getenv("ZVONOK_OWNER_NAME", "")),
        # Russian needs the genitive ("ассистент Ивана", "по поручению Ивана").
        # The nominative above cannot be inflected reliably in code, and a
        # mis-declined owner name in the very first sentence sounds wrong to a
        # native ear — so it is configured, not derived.
        "owner_gen": os.getenv(
            "ZVONOK_OWNER_NAME_RU_GEN",
            os.getenv("ZVONOK_OWNER_NAME_RU", os.getenv("ZVONOK_OWNER_NAME", "")),
        ),
        "owner_full": os.getenv(
            "ZVONOK_OWNER_FULLNAME_RU", os.getenv("ZVONOK_OWNER_FULLNAME", "")
        ),
        "role": "AI-ассистент",
    },
    "es": {
        "assistant": os.getenv("ZVONOK_ASSISTANT_NAME", "Klava"),
        "owner": os.getenv("ZVONOK_OWNER_NAME", ""),
        "owner_full": os.getenv("ZVONOK_OWNER_FULLNAME", ""),
        "role": "asistente de IA",
    },
}

# Contact numbers for when a booking or callback genuinely needs one.
# Prompt-only data — never spoken unless the task calls for it.
#
#   booking phone — the number the call is placed FROM. The right thing to
#     leave with a restaurant or a clinic (reachable by call and SMS).
#   messenger phone — the owner's personal messenger number (WhatsApp etc.),
#     given out only when the callee specifically needs a messenger.
#   messenger phone note — an optional dictation hint spoken to the agent's
#     prompt, e.g. "it has five zeros in a row — people mishear it".
BOOKING_PHONE = os.getenv("ZVONOK_BOOKING_PHONE", "")
MESSENGER_PHONE = os.getenv("ZVONOK_MESSENGER_PHONE", "")
MESSENGER_PHONE_NOTE = os.getenv("ZVONOK_MESSENGER_PHONE_NOTE", "")

# Who the assistant says it is calling FOR when the callee is a stranger.
# Naming the owner up front is worse than useless — "assistant of Ivan" means
# nothing to a restaurant and reads as cold-call — while "of your potential
# client" tells them exactly why the call is worth their time. Per-call
# metadata `introduce_as` overrides this (e.g. "вашего постоянного клиента"
# for somewhere the owner actually frequents, or "Ивана" for a callee who
# knows him). Russian value must arrive ALREADY in the genitive, same reason
# as owner_gen above.
INTRODUCE_DEFAULT = {
    "en": "a potential client",
    "ru": "вашего потенциального клиента",
    "es": "un cliente potencial",
}

# The §8 disclosure as FIXED text, per language. This exists because on a real
# call the model's spoken introduction was shredded by barge-in — it came out as
# "This" / "This is Klava, an" / "on behalf of ... This call is transcribed", and
# the phrase identifying it as an AI assistant was never heard at all. A legal
# requirement cannot be left to a generative model competing with line noise, so
# it is spoken verbatim and uninterruptibly.
#
# Two levels, because "I am an AI" and "your words are being kept" are different
# disclosures: knowing you are talking to a machine does not tell you anything is
# stored. Both levels therefore state the storage fact — what changes is the
# register.
#
#   light (default) — the storage fact in the words a human receptionist would
#     use. We keep TEXT, not audio (§5.3 has audio recording off), so "I'll note
#     down your answer" is both gentler and more accurate than "this call is
#     recorded", which people hear as surveillance and which frightens them into
#     hanging up.
#   full — explicit transcription-and-storage wording plus an offer to stop, for
#     jurisdictions or task types that warrant it (two-party-consent rules,
#     anything that books or commits on someone's behalf).
#
# Which level applies where becomes a policy table in phase 2 (§8).
DISCLOSURE_LIGHT = {
    "en": "This is {assistant}, an AI assistant calling for {on_behalf}. I'll note down your answer so nothing gets lost.",
    "ru": "Это {assistant}, AI-ассистент {on_behalf}. Запишу ответ, чтобы ничего не упустить.",
    "es": "Soy {assistant}, un asistente de IA que llama de parte de {on_behalf}. Anotaré su respuesta para no perder nada.",
}
DISCLOSURE_FULL = {
    "en": "This is {assistant}, an AI assistant calling on behalf of {on_behalf}. This call is transcribed and stored. If you would rather it were not, say so and I'll end the call.",
    "ru": "Это {assistant}, AI-ассистент, звоню по поручению {on_behalf}. Разговор транскрибируется и сохраняется. Если вы против — скажите, и я завершу звонок.",
    "es": "Soy {assistant}, un asistente de IA que llama de parte de {on_behalf}. Esta llamada se transcribe y se guarda. Si prefiere que no, dígamelo y finalizaré la llamada.",
}
DISCLOSURE_LEVELS = ("light", "full")
# The probe, also fixed text. Asking the model for "one or two words" produced
# "I'm listening — please go ahead", which sounds like an IVR prompt: the model
# will not reliably honour a length instruction, so take it out of the loop.
PROBE = {"en": "Hello?", "ru": "Алло?", "es": "¿Hola?"}


def disclosure_for(
    language: str, level: str = "light", introduce_as: str | None = None
) -> str:
    ident = identity_for(language)
    table = DISCLOSURE_FULL if level == "full" else DISCLOSURE_LIGHT
    template = table.get(language, table["en"])
    on_behalf = introduce_as or INTRODUCE_DEFAULT.get(language, INTRODUCE_DEFAULT["en"])
    return template.format(assistant=ident["assistant"], on_behalf=on_behalf)


# Words that mean "what you say is being kept". Both disclosure levels must
# convey this — the whole point of the two levels is register, not content.
_STORAGE_WORDS = (
    "note down", "make a note", "transcrib", "recorded", "stored",
    "запишу", "записыв", "транскриб", "сохран",
    "anotar", "anotaré", "transcrib", "guarda",
)
_AI_WORDS = ("ai assistant", "ai-ассистент", "ai ассистент", "asistente de ia")


def disclosure_delivered(
    turns: list[dict[str, Any]], language: str, level: str = "light"
) -> bool:
    """Did a complete disclosure actually get spoken, in one piece?

    Checked against what the agent SAID, not what it intended: a turn truncated
    by barge-in does not count, which is the whole point. Requires, in a single
    uninterrupted turn: the assistant's name, that it is an AI, and that the
    answer is being kept. The owner is deliberately NOT required: the
    introduction now names a role ("assistant of a potential client"), not a
    person — a stranger's name up front is noise to the callee and needless
    disclosure of the owner's identity.

    ⚠ The `interrupted` check below is the part that makes this true rather than
    merely claimed. Without it the function only matched substrings, so an
    introduction that was cut off mid-sentence — the exact phase-1 failure this
    guard exists to prevent — still counted as delivered, because the stored
    turn text contains words the callee never heard.
    """
    ident = identity_for(language)

    for t in turns:
        if t["speaker"] != "assistant":
            continue
        if t.get("interrupted"):
            continue
        low = t["text"].lower()
        if ident["assistant"].lower() not in low:
            continue
        if not any(a in low for a in _AI_WORDS):
            continue
        if not any(s in low for s in _STORAGE_WORDS):
            continue
        return True
    return False


def identity_for(language: str) -> dict[str, str]:
    return IDENTITY.get(language, IDENTITY["en"])


# The agent's disposition vocabulary mapped onto BRIEF §4 terminal call states.
# Writing "completed" for anything that merely reached the shutdown callback
# would feed the phase-2 state machine lies: a timed-out call, or one that
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


# Voicemail greetings, narrowly matched. This is a SAFETY NET under the prompt,
# not the primary mechanism — but the prompt alone demonstrably failed to hang up
# on a real answering machine, and every second of that is billed.
VOICEMAIL_PHRASES = (
    "leave a message", "leave your message", "leave your name", "after the beep",
    "at the tone", "is not available", "not available right now", "voicemail",
    "voice mail", "record your message", "unable to take your call",
    # A real greeting we hit said "leave your email so I can write you back" —
    # never the word "message". Phrase matching is inherently whack-a-mole; it is
    # the net, not the mechanism. The real fix is not introducing ourselves
    # before we have heard who answered.
    "leave your email", "write you back", "better to reach us",
    "оставьте сообщение", "оставьте свое сообщение", "оставьте своё сообщение",
    "после сигнала", "автоответчик", "не может ответить", "недоступен",
    "deje su mensaje", "deje un mensaje", "después de la señal", "buzón de voz",
    "no está disponible",
)
# A call screener ALSO says "after the tone" — but it asks for your name and says
# it will connect you. Misreading a screener as voicemail would hang up on a call
# that was about to reach a human, so these veto the match.
SCREENER_PHRASES = (
    "state your name", "say your name", "who's calling", "who is calling",
    "connect you", "screening", "reduce spam",
)


def looks_like_voicemail(text: str) -> bool:
    low = text.lower()
    if any(s in low for s in SCREENER_PHRASES):
        return False
    return any(p in low for p in VOICEMAIL_PHRASES)


def facts_block(answer_schema: dict[str, Any] | None) -> str:
    """Turn the caller's answer_schema into "things to ask about".

    The schema drives BOTH ends (BRIEF §9 phase-2 trap 8). Giving it only to the
    extractor is the classic failure: the extractor dutifully reports null for a
    required field because the agent never thought to ask the question. The
    field's `description` is what the requesting agent wrote for a reader, so it
    is exactly the right prompt text.
    """
    props = (answer_schema or {}).get("properties")
    if not isinstance(props, dict) or not props:
        return ""

    lines = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        description = spec.get("description") or name.replace("_", " ")
        lines.append(f"- {name}: {description}")

    return f"""
## Facts to capture
Someone will read your call back for these specific things. Work them into the
conversation naturally, one question at a time — do not read this list out:

{chr(10).join(lines)}

Missing one means the call has to be made again, so do not hang up while any of
them is still open and askable. If a fact simply is not available from this
person, that is a fine answer — ask who would know.
"""


def principal_details_block(ident: dict[str, str]) -> str:
    """The owner's name and numbers, or an honest 'you have none' fallback.

    Everything here is env-provided; an unconfigured deployment must degrade to
    an agent that promises a confirmation rather than one that invents a name
    or reads out an empty string.
    """
    lines = []
    if ident.get("owner"):
        line = f"- Name for a booking: {ident['owner']}."
        if ident.get("owner_full"):
            line += (
                f" Only if they insist on a full name: {ident['owner_full']}."
            )
        lines.append(line)
    if BOOKING_PHONE:
        lines.append(
            f"- Phone for a booking or callback: the number you are calling "
            f"from, {BOOKING_PHONE}. It takes calls and SMS."
        )
    if MESSENGER_PHONE:
        note = f" Careful: {MESSENGER_PHONE_NOTE}." if MESSENGER_PHONE_NOTE else ""
        lines.append(
            f"- Only if they specifically need WhatsApp or another messenger: "
            f"{MESSENGER_PHONE}.{note}"
        )
    if not lines:
        return (
            "The goal text is your only source of the client's details. If a "
            "booking needs a name or a number you do not have, say the client "
            "will confirm it themselves — never invent one."
        )
    lines.append(
        "- When you give a phone number, dictate it slowly, digit by digit, "
        "and have them read it back before treating it as passed on."
    )
    return "\n".join(lines)


def build_instructions(
    goal: str,
    language: str,
    *,
    s2s: bool = True,
    disclosure_level: str = "light",
    answer_schema: dict[str, Any] | None = None,
    introduce_as: str | None = None,
) -> str:
    """System prompt.

    Saying "AI assistant" is deliberate and load-bearing: it satisfies the §8
    disclosure requirement in the introduction itself, rather than depending on
    the model to admit it later when asked.
    """
    ident = identity_for(language)
    lang_name = LANGUAGE_NAMES.get(language, "English")
    on_behalf = introduce_as or INTRODUCE_DEFAULT.get(language, INTRODUCE_DEFAULT["en"])

    # The original no-autodetect rule (BRIEF §5.3) existed because autodetect
    # wrecks STT model selection. A speech-to-speech model has no separate STT,
    # so the rule is relaxed for S2S profiles only.
    language_rule = (
        f"Speak {lang_name}. If the person clearly answers in English, Russian or "
        f"Spanish instead, switch to that language and stay in it for the rest of "
        f"the call. Do not switch for a single stray word — only when they are "
        f"plainly speaking another language."
        if s2s
        else f"Speak {lang_name} for the entire call. Never switch languages."
    )

    return f"""You are {ident['assistant']}, an {ident['role']} making a phone call on behalf of a client. On this call you introduce yourself as the {ident['role']} of {on_behalf} — that phrasing is what you lead with. Do not volunteer the client's name before the conversation actually needs it (see "Your principal's details").

## Goal
{goal}

The goal above is written for you, not for the callee — it may describe them in
the third person ("find out what hookah he likes"). Never read it out as
written. Speak to the person directly, in the second person, in your own words:
"what hookah do you like?" Ask for one thing at a time rather than reciting a
list of fields.
{facts_block(answer_schema)}
## Your principal's details
Use these ONLY once the task genuinely needs them — a booking that wants a name,
a callback number to leave. Never volunteer them in the introduction.

{principal_details_block(ident)}

## Language
{language_rule}

## Who picks up
Do not assume a person answered. Listen to the first thing you hear and adapt:

**A real person** — says something short, then STOPS AND WAITS for you. That
pause is the signal, not the words. It may be a greeting, a bare "yes", a
company name, a surname, a grunt, or something you did not catch at all; every
language and every household does this differently and you know those
conventions better than any list. What identifies a person is the shape of the
turn: brief, addressed to you, and then silence inviting your reply.

A machine has the opposite shape: it keeps talking through a scripted passage
and never yields the turn. Judge by that shape, not by specific words.
Introduce yourself in ONE breath, then go straight to the point. Use this
sentence, near enough word for word — it is the required disclosure and it is
deliberately worded to be brief and unalarming:
"{disclosure_for(language, disclosure_level)} <your question>"
Keep it under about eight seconds. Do not deliver a speech, and do not pad the
disclosure with extra reassurance — that is what makes people uneasy.

**A call-screening service** — answers instantly and asks *who is calling*,
often saying it will pass you through or connect you. It speaks a complete
scripted sentence rather than a short greeting. This is NOT a conversation: it
records a short label and announces it to the human as "You have a call from
___".

Say ONLY your name. Four words, no greeting, no sentence, no purpose:
"AI assistant {ident['assistant']}."

Then stop talking. Anything longer than a name gets truncated or garbled in
that announcement, and the person hears nonsense and declines the call. Do not
mention who you are calling for, the transcription, or your question at this
stage — none of it survives. Only if the machine explicitly asks the *reason*
as well, add one short noun phrase and nothing more.

Then WAIT. The human arrives later and sounds like a fresh "Hello?", often
talking over you. When that happens: STOP, let them finish, then INTRODUCE
YOURSELF IN FULL — your name, that you are the {ident['role']} of {on_behalf},
that the call is noted down. They heard none of the exchange with the screener.
Only then ask your question.

**Voicemail or an answering machine** — a recording that plays straight through
and then invites you to leave something after a tone. It never pauses for you.
Do NOT leave a message and do NOT keep talking. Immediately call
mark_unreachable with reason "voicemail".

**An automated menu (IVR)** — it lists options and asks you to press keys. Do
not introduce yourself to a machine; just navigate with send_dtmf to reach a
human. Try at most three menu levels; if no human is reachable, call
mark_unreachable with reason "ivr_deadend". Once a human answers, introduce
yourself in full as above.

## Conduct
If asked whether you are an AI, say yes plainly. Never claim to be a person, and
never imply you are the client yourself — you are an assistant calling on their
behalf.{f" If they ask who your client is by name, it is fine to say {ident['owner']}." if ident.get("owner") else ""}

If they object to being recorded or transcribed: apologise, say out loud that
you are ending the call and nothing will be kept, and only then call
mark_unreachable with reason "declined". Never end the call silently — they
asked you something and deserve an answer. This is honoured literally: the
transcript is discarded.

**Do not mistake confusion for refusal.** A short sound, a word you did not
catch, a "what?", "huh?", "шо?", "¿qué?", or a bad line is NOT an objection and
NOT an answer. It almost always means they did not hear you. Say your
introduction again — once, slower and shorter — and carry on. Only treat it as a
refusal if they say something that plainly means no. Ending a call on a syllable
you did not understand is far worse than asking someone to repeat themselves.

**If they answer in another language you speak** — Russian, Spanish or English —
switch to it and give your introduction again in that language. Do not treat a
language mismatch as a failed call; it is the commonest thing that happens when
a number reaches a real person.

Be brief and polite — you are interrupting someone's day. If they cannot help,
ask who can.

**Stay near the goal.** People joke, test you, and wander off topic. Acknowledge
it in a few words — warmly, without playing along at length — and steer straight
back to what you called about. Do not answer riddles, do not follow a tangent
into a new subject, and do not repeat back a joke as if it were an answer. If
they ask you to say something unrelated, decline lightly and return to the
question. One short acknowledgement, then back on track.

**Numbers must be confirmed, not just heard.** Whenever you get a time, price,
date, phone number or quantity: say it back to them, get their agreement, and
then call `record_answer` with it. The line is 8 kHz and mishears digits
constantly — an unconfirmed number is worse than no number, because it looks
like an answer. `end_call` will refuse until you have done this.

You are on an 8 kHz phone line, so expect imperfect audio. If something is
unclear, ask once for it to be repeated rather than guessing.

When the goal is achieved or is clearly unachievable, thank them and call
end_call. Never stay on the line to fill silence."""


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
        # Facts the agent claims to have confirmed out loud. Phase 2's extractor
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
        # Learned on the phase-2 acceptance call: the callee said "шо" — one
        # Russian syllable meaning roughly "what?" — on an English call, and the
        # model read that as a refusal to be recorded, discarded everything and
        # hung up without explaining itself. A confused noise is not consent
        # withdrawal.
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
    `supports_say=False`, so say() raises unless a TTS is also attached. Adding
    a TTS purely for these two lines would put a second, different-sounding
    voice on the call. So the fixed text goes through generate_reply with a
    verbatim instruction instead.

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

    goal = meta.get("goal") or DEFAULT_GOAL
    language = meta.get("language", "en")
    caller_id = meta.get("caller_id")
    job_id = meta.get("job_id") or ctx.job.id
    # "light" by default (§8): the storage fact in plain words. "full" adds
    # explicit transcribe-and-store wording plus an offer to stop, for
    # jurisdictions or task types that warrant it. Phase 2 picks this from a
    # policy table; here it is simply passed through.
    disclosure_level = meta.get("disclosure_level", "light")
    if disclosure_level not in DISCLOSURE_LEVELS:
        logger.warning("unknown disclosure_level %r — using light", disclosure_level)
        disclosure_level = "light"
    # Who to say the call is for ("your regular customer", "Ивана"). Free text
    # chosen by the requesting agent from the task's context; for Russian it
    # must already be in the genitive. Bounded because it is spliced into both
    # the system prompt and the verbatim disclosure line.
    introduce_as = meta.get("introduce_as")
    if introduce_as is not None:
        introduce_as = str(introduce_as).strip()[:120] or None
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

    session = AgentSession(
        llm=xai.realtime.RealtimeModel(
            voice="ara",
            turn_detection=TurnDetection(
                type="server_vad",
                # 0.5 is tuned for clean wideband audio. On 8 kHz G.711 with line
                # noise it false-triggers, and every false trigger is an
                # interruption: the observed "speech not done in time after
                # interruption" storms and truncated turns are that, not an
                # end-of-turn problem.
                threshold=BARGE_IN_THRESHOLD,
                prefix_padding_ms=300,
                silence_duration_ms=END_OF_TURN_SILENCE_MS,
                create_response=True,
                interrupt_response=True,
            ),
        ),
    )

    # Live transcript capture (the list is created above, before the agent, so
    # both share it). We collect as we go rather than only dumping
    # session.history at the end, so a mid-call crash still leaves evidence.
    t_zero = time.monotonic()
    callee_spoke = False
    last_activity = time.monotonic()
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
        degrades to a delayed reconciliation instead of a call that appears
        never to have happened (BRIEF §9 phase-2 trap 4).

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
        # Hang up if nothing else already has. The comment above promised
        # teardown but the callback only wrote a transcript: an exception raised
        # after the callee picked up (the opening probe failing, say) would end
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
        nonlocal callee_spoke, last_activity
        if getattr(ev, "new_state", None) == "speaking":
            callee_spoke = True
            last_activity = time.monotonic()

    @session.on("conversation_item_added")
    def _on_item(ev: Any) -> None:  # noqa: ANN401 — event shape varies by version
        nonlocal last_activity, voicemail_hangup
        item = getattr(ev, "item", ev)
        text = getattr(item, "text_content", None) or ""
        if not text:
            return
        last_activity = time.monotonic()
        speaker = getattr(item, "role", "unknown")
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
    # one (BRIEF §9 phase-0: ×20–34 price swing). Falls back to the trunk default.
    if caller_id:
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
        # A fixed word, not a free generation. Asked for "one or two words", the
        # model instead produced "I'm listening — please go ahead", which sounds
        # exactly like an IVR prompt and primes the callee to treat us as a
        # machine. Quoting the exact word holds far better than a length limit.
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
            agent_name="zvonok-caller",
            load_fnc=concurrency_load,
            load_threshold=1.0,
            # The worker's health/debug HTTP server defaults to 0.0.0.0:8081.
            # de1 is a shared host and matomo already owns 8081 — and since this
            # container uses host networking, that collision is fatal at startup.
            host="127.0.0.1",
            port=int(os.getenv("ZVONOK_AGENT_PORT", "18130")),
        )
    )
