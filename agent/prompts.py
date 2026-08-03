"""Everything the agent says, and everything that checks it said it.

Two kinds of text live here and they are not the same kind of thing:

  FIXED TEXT   — the disclosure and the probe. Spoken as close to verbatim as a
                 speech-to-speech model allows, because both were demonstrably
                 mangled when left to the model. The disclosure is a legal
                 requirement (§8); it cannot depend on winning a race against an
                 impatient human and line noise.
  INSTRUCTIONS — the system prompt. Judgment, in prose, for a model to apply.

The module is pure text and `os.getenv`. It deliberately imports nothing from
LiveKit or the realtime stack: this is the part that changes after every call
that goes wrong, and it must be testable in a bare interpreter.

The owner's personal details come EXCLUSIVELY from the environment (configured
on the deploy host, never in this repository — the same rule as secrets). Unset
values simply drop out of the prompt: an unconfigured deployment degrades to an
agent that promises a confirmation, never one that invents a name.
"""

from __future__ import annotations

import os
from typing import Any

# Names are spelled for PRONUNCIATION, not for paperwork: a Cyrillic "Иван" is
# read correctly by the model on a Russian call, the Latin spelling is not.
LANGUAGE_NAMES = {
    "en": "English", "ru": "Russian", "es": "Spanish", "pl": "Polish",
}

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
    "pl": {
        # Polish has no native "v": the model reads "Klava" as a foreign word and
        # transcription comes back "Klawa" about as often as not — which the
        # disclosure check would then fail to recognise. Spelled the Polish way
        # by default, with assistant_variants() covering the other spelling.
        "assistant": os.getenv("ZVONOK_ASSISTANT_NAME_PL", "Klawa"),
        "owner": os.getenv("ZVONOK_OWNER_NAME", ""),
        "owner_full": os.getenv("ZVONOK_OWNER_FULLNAME", ""),
        "role": "asystent AI",
    },
}

# Contact numbers for when a booking or callback genuinely needs one.
# Prompt-only data — never spoken unless the task calls for it.
#
#   booking phone — the number the call is placed FROM. The right thing to leave
#     with a restaurant or a clinic (reachable by call and SMS).
#   messenger phone — the owner's personal messenger number, given out only when
#     the callee specifically needs a messenger.
#   messenger phone note — a dictation hint, e.g. "it has five zeros in a row".
BOOKING_PHONE = os.getenv("ZVONOK_BOOKING_PHONE", "")
MESSENGER_PHONE = os.getenv("ZVONOK_MESSENGER_PHONE", "")
MESSENGER_PHONE_NOTE = os.getenv("ZVONOK_MESSENGER_PHONE_NOTE", "")

# Who the assistant says it is calling FOR when the callee is a stranger. Naming
# the owner up front is worse than useless — "assistant of Ivan" means nothing to
# a restaurant and reads as a cold call — while "of your potential client" tells
# them why the call is worth their time. Per-call `introduce_as` overrides this.
# The Russian value must arrive ALREADY in the genitive, same reason as owner_gen.
INTRODUCE_DEFAULT = {
    "en": "a potential client",
    "ru": "вашего потенциального клиента",
    "es": "un cliente potencial",
    "pl": "potencjalnego klienta",
}

# --- the §8 disclosure ------------------------------------------------------
#
# Fixed text, per language, because on a real call the model's spoken
# introduction was shredded by barge-in: it came out as "This" / "This is Klava,
# an" / "on behalf of ... This call is transcribed", and the phrase identifying
# it as an AI was never heard at all.
#
# Three levels, because "I am an AI" and "your words are being kept" are
# different disclosures — knowing you are talking to a machine tells you nothing
# about retention. ALL THREE carry both facts; what varies is everything around
# them.
#
#   brief — the shortest rung: the AI fact, the retention fact, nothing else.
#     For one-question canvassing where the callee is a counter clerk taking
#     their tenth call of the hour. No principal and no name, deliberately: a
#     client they have never heard of costs three seconds and buys nothing, and
#     a name nobody recognises invites "Klawa? which Klawa?" instead of an
#     answer. This is a genuine thinning of §8, which is why it is never chosen
#     automatically — only by a requester who knows the call is a stock check.
#   light (default) — the retention fact in the words a human receptionist would
#     use. We keep TEXT, not audio, so "I'll note down your answer" is both
#     gentler and more accurate than "this call is recorded", which people hear
#     as surveillance and which frightens them into hanging up.
#   full — explicit transcription-and-storage wording plus an offer to stop, for
#     two-party-consent jurisdictions and anything that commits on someone's
#     behalf.
DISCLOSURE_BRIEF = {
    "en": "Hello, this is an AI assistant, I'll note the answer down.",
    "ru": "Здравствуйте, это AI-ассистент, запишу ответ.",
    "es": "Hola, soy un asistente de IA, anotaré la respuesta.",
    "pl": "Dzień dobry, tu asystent AI, zanotuję odpowiedź.",
}
DISCLOSURE_LIGHT = {
    "en": "This is {assistant}, an AI assistant calling for {on_behalf}. I'll note down your answer so nothing gets lost.",
    "ru": "Это {assistant}, AI-ассистент {on_behalf}. Запишу ответ, чтобы ничего не упустить.",
    "es": "Soy {assistant}, un asistente de IA que llama de parte de {on_behalf}. Anotaré su respuesta para no perder nada.",
    "pl": "Dzień dobry, tu {assistant}, asystent AI dzwoniący w imieniu {on_behalf}. Zanotuję odpowiedź, żeby nic nie umknęło.",
}
DISCLOSURE_FULL = {
    "en": "This is {assistant}, an AI assistant calling on behalf of {on_behalf}. This call is transcribed and stored. If you would rather it were not, say so and I'll end the call.",
    "ru": "Это {assistant}, AI-ассистент, звоню по поручению {on_behalf}. Разговор транскрибируется и сохраняется. Если вы против — скажите, и я завершу звонок.",
    "es": "Soy {assistant}, un asistente de IA que llama de parte de {on_behalf}. Esta llamada se transcribe y se guarda. Si prefiere que no, dígamelo y finalizaré la llamada.",
    "pl": "Dzień dobry, tu {assistant}, asystent AI dzwoniący w imieniu {on_behalf}. Rozmowa jest transkrybowana i zapisywana. Jeśli sobie Państwo tego nie życzą, proszę powiedzieć, a zakończę rozmowę.",
}
DISCLOSURE_LEVELS = ("brief", "light", "full")

_DISCLOSURE_TABLES = {
    "brief": DISCLOSURE_BRIEF,
    "light": DISCLOSURE_LIGHT,
    "full": DISCLOSURE_FULL,
}

# The probe, also fixed text. Asked for "one or two words like Hello?", the model
# produced "I'm listening — please go ahead", which sounds like an IVR prompt and
# primes the callee to treat us as a machine. The model will not reliably honour
# a length instruction, so take it out of the loop.
PROBE = {"en": "Hello?", "ru": "Алло?", "es": "¿Hola?", "pl": "Halo?"}


def identity_for(language: str) -> dict[str, str]:
    return IDENTITY.get(language, IDENTITY["en"])


def disclosure_for(
    language: str, level: str = "light", introduce_as: str | None = None
) -> str:
    ident = identity_for(language)
    table = _DISCLOSURE_TABLES.get(level, DISCLOSURE_LIGHT)
    template = table.get(language, table["en"])
    on_behalf = introduce_as or INTRODUCE_DEFAULT.get(language, INTRODUCE_DEFAULT["en"])
    return template.format(assistant=ident["assistant"], on_behalf=on_behalf)


# --- did the disclosure actually land? --------------------------------------

# Words that mean "what you say is being kept". Every level must convey this —
# the point of the levels is register, not content.
_STORAGE_WORDS = (
    "note down", "note the answer", "make a note", "transcrib", "recorded", "stored",
    "запишу", "записыв", "транскриб", "сохран",
    "anotar", "anotaré", "transcrib", "guarda",
    "zanotuj", "zanotuję", "zapisz", "zapiszę", "transkryb", "notuj",
)
_AI_WORDS = (
    "ai assistant", "ai-ассистент", "ai ассистент", "asistente de ia",
    "asystent ai", "asystentka ai",
)


def assistant_variants(name: str) -> set[str]:
    """Spellings of the assistant's name that count as "it said its name".

    Polish has no native "v", so a spoken "Klawa" comes back transcribed as
    "Klava" roughly as often as the other way round. Requiring an exact match
    would make the disclosure look undelivered on a call where it plainly was,
    and the guard would then talk over the callee to repeat it.
    """
    low = name.lower()
    return {low, low.replace("w", "v"), low.replace("v", "w")}


def disclosure_delivered(
    turns: list[dict[str, Any]], language: str, level: str = "light"
) -> bool:
    """Did a complete disclosure actually get spoken, in one piece?

    Checked against what the agent SAID, not what it intended. Requires, in a
    single uninterrupted turn: that it is an AI, that the answer is being kept,
    and — at `light` and `full` — the assistant's name.

    The owner is deliberately NOT required at any level: the introduction names a
    role ("assistant of a potential client"), not a person.

    At `brief` the assistant's own name is not required either, because the brief
    disclosure does not contain one. Keeping the name in this check while
    removing it from the line the agent speaks would leave the guard convinced
    the disclosure never landed, and it would cut across the callee to say it
    again — the exact failure this function exists to prevent.

    ⚠ The `interrupted` check is the part that makes this true rather than merely
    claimed. Without it the function only matched substrings, so an introduction
    cut off mid-sentence still counted as delivered, because the stored turn text
    contains words the callee never heard.
    """
    ident = identity_for(language)

    for t in turns:
        if t["speaker"] != "assistant":
            continue
        if t.get("interrupted"):
            continue
        low = t["text"].lower()
        if level != "brief" and not any(
            v in low for v in assistant_variants(ident["assistant"])
        ):
            continue
        if not any(a in low for a in _AI_WORDS):
            continue
        if not any(s in low for s in _STORAGE_WORDS):
            continue
        return True
    return False


# --- system prompts ---------------------------------------------------------


def facts_block(answer_schema: dict[str, Any] | None, brief: bool = False) -> str:
    """Turn the caller's answer_schema into "things to ask about".

    The schema drives BOTH ends. Giving it only to the extractor is the classic
    failure: the extractor dutifully reports null for a required field because
    the agent never thought to ask the question. The field's `description` is
    what the requesting agent wrote for a reader, so it is exactly the right
    prompt text.
    """
    props = (answer_schema or {}).get("properties")
    if not isinstance(props, dict) or not props:
        return ""

    lines = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        description = spec.get("description") or name.replace("_", " ")
        lines.append(f"- {name}: {description}")

    if brief:
        # The standard wording below ("do not hang up while any of them is still
        # open") is exactly wrong for a canvass: with five fields it turns a
        # fifteen-second stock check into an interrogation, and the callee hangs
        # up somewhere in the middle with nothing captured. On this kind of call
        # the FIRST field is the call; the rest are a bonus.
        return f"""
## Facts to capture
In this order — the first one is what the call is for:

{chr(10).join(lines)}

Stop as soon as an answer makes the rest pointless, and stop anyway the moment
they sound like they want to get back to work. Leaving fields unanswered is
normal here: a fifteen-second call that got the first answer is a success, an
interrogation that got all five is not. Never read this list out.
"""

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
    """The owner's name and numbers, or an honest 'you have none' fallback."""
    lines = []
    if ident.get("owner"):
        line = f"- Name for a booking: {ident['owner']}."
        if ident.get("owner_full"):
            line += f" Only if they insist on a full name: {ident['owner_full']}."
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


def build_canvass_instructions(
    goal: str,
    language: str,
    *,
    disclosure_level: str = "brief",
    answer_schema: dict[str, Any] | None = None,
) -> str:
    """The one-question canvass prompt: short on purpose.

    Length is a LATENCY COST HERE IN A WAY IT IS NOT ELSEWHERE. A long system
    prompt normally only slows the first turn, because every later turn rides the
    provider's KV cache — but a canvass IS a first turn. Fifteen calls, fifteen
    cold starts, and the callee sits in silence for every one of them. So this is
    a separate template rather than the full prompt with sections switched off:
    the general one carries branches (call screeners, bookings, dictating a phone
    number back, staying on topic through a chat) that a thirty-second stock
    check will never reach, and each is paid for on every call.

    What is NOT trimmed, at any length: the §8 disclosure, admitting to being an
    AI when asked, and honouring a refusal to be transcribed.
    """
    ident = identity_for(language)
    lang_name = LANGUAGE_NAMES.get(language, "English")

    return f"""You are an {ident['role']} making a short phone call for a client. ONE question, one answer, then you hang up. Under a minute.

## Your question
{goal}

This is written for you, not to be read out. Ask it in your own words, in the second person, one thing at a time.
{facts_block(answer_schema, brief=True)}
## What you say first
The moment a HUMAN speaks, say this and only this, in one breath:
"{disclosure_for(language, disclosure_level)} <your question>"

This first turn is fixed. Whatever you think you heard, you say this. Do not react to their greeting, do not comment on it, and never answer it.

## What you never do
You are not a helpline. Never explain, advise, instruct, or answer a question of theirs. On an 8 kHz line you will sometimes hear a fluent sentence nobody said — often in another language, often a question. The tell is that it does not fit: a business does not answer its own phone by asking YOU for advice. When it does not fit, you misheard, so ask your question again instead of replying to it.

No small talk, no offering help, no apologising for calling, no asking the same thing twice in different words. Do not give your name unless asked (it is {ident['assistant']}).

## Who picked up
- **A person** — says something short and stops. Speak your line above.
- **A recording that offers you options** ("press 1…") — a human is behind it. Listen to the WHOLE menu, then send_dtmf once for the option nearest your goal; if none fits, 0, then 9. If it says to stay on the line, say nothing and wait — that is the correct action. Say nothing to any of it. After three menus or two presses that change nothing, mark_unreachable "ivr_deadend".
- **Hold music or silence after a menu** — you are in a queue. Stay silent. Do not talk to it.
- **A recording that just talks and then invites a message** — voicemail. Leave nothing, say nothing, mark_unreachable "voicemail" at once.

## Language
Speak {lang_name}. If they plainly answer in another language you speak, switch to it and say your line again there.

## Honesty (not optional, however short the call)
If asked whether you are an AI, say yes plainly. Never claim to be a person. If they object to being recorded or noted down, say out loud that you are ending the call and nothing will be kept, then mark_unreachable "declined".

A "what?" or a syllable you did not catch is confusion, not refusal — say your line once more, shorter.

## Finishing
Any answer ends the call, including "no" and "we don't know". Read the one fact back, call record_answer, thank them in three words, call end_call. A fifteen-second "no" is a success."""


def build_instructions(
    goal: str,
    language: str,
    *,
    s2s: bool = True,
    disclosure_level: str = "light",
    answer_schema: dict[str, Any] | None = None,
    introduce_as: str | None = None,
) -> str:
    """The conversational system prompt.

    Saying "AI assistant" is deliberate and load-bearing: it satisfies the §8
    disclosure requirement in the introduction itself, rather than depending on
    the model to admit it later when asked.

    For `brief` this delegates to the much shorter canvass template — see
    build_canvass_instructions for why length is specifically expensive there.
    """
    if disclosure_level == "brief":
        return build_canvass_instructions(
            goal, language,
            disclosure_level=disclosure_level,
            answer_schema=answer_schema,
        )

    ident = identity_for(language)
    lang_name = LANGUAGE_NAMES.get(language, "English")
    on_behalf = introduce_as or INTRODUCE_DEFAULT.get(language, INTRODUCE_DEFAULT["en"])
    # NOTE: `brief` never reaches here — it returns above, from the canvass
    # template. Everything below is the conversational path only. An earlier
    # version carried `if brief` branches here as well; once the early return
    # went in they became unreachable, and unreachable branches that LOOK like
    # the brief path are a trap — the next safety fix gets written into them and
    # silently never runs on a real canvass call.
    opening = (
        f"""You are {ident['assistant']}, an {ident['role']} making a phone call on behalf of a client. On this call you introduce yourself as the {ident['role']} of {on_behalf} — that phrasing is what you lead with. Do not volunteer the client's name before the conversation actually needs it (see "Your principal's details")."""
    )

    person_branch = f"""Introduce yourself in ONE breath, then go straight to the point. Use this
sentence, near enough word for word — it is the required disclosure and it is
deliberately worded to be brief and unalarming:
"{disclosure_for(language, disclosure_level)} <your question>"
Keep it under about eight seconds. Do not deliver a speech, and do not pad the
disclosure with extra reassurance — that is what makes people uneasy."""

    # What to do when a human finally arrives — after a screener, after a menu,
    # after being handed over. It is the same introduction as at pickup.
    screener_return = (
        f"INTRODUCE YOURSELF IN FULL — your name, that you are the "
        f"{ident['role']} of {on_behalf}, that the call is noted down, and then "
        f"your question."
    )

    # Language switching is allowed on a speech-to-speech profile only. The
    # no-autodetect rule (BRIEF §5.3) is about the ASR language hint, which is
    # pinned per call; a cascade profile picks a whole STT model from it and
    # cannot switch mid-call, so profile B stays hard-locked.
    spoken = [n for code, n in LANGUAGE_NAMES.items() if code != language]
    others = f"{', '.join(spoken[:-1])} or {spoken[-1]}"
    language_rule = (
        f"Speak {lang_name}. If the person clearly answers in {others} instead, "
        f"switch to that language and stay in it for the rest of the call. Do not "
        f"switch for a single stray word — only when they are plainly speaking "
        f"another language."
        if s2s
        else f"Speak {lang_name} for the entire call. Never switch languages."
    )

    return f"""{opening}

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
{person_branch}

**A call-screening service** — answers instantly and asks *who is calling*,
often saying it will pass you through or connect you. It speaks a complete
scripted sentence rather than a short greeting. This is NOT a conversation: it
records a short label and announces it to the human as "You have a call from
___".

⚠ The test is that it ASKS FOR YOUR NAME. A recording that merely talks at you —
a welcome, a data-protection notice, a list of options — is not a screener, it
is the menu described below, and answering it with your name achieves nothing
but noise. If nobody asked who is calling, this branch does not apply.

Say ONLY your name. Four words, no greeting, no sentence, no purpose:
"AI assistant {ident['assistant']}."

Then stop talking. Anything longer than a name gets truncated or garbled in
that announcement, and the person hears nonsense and declines the call. Do not
mention who you are calling for, the transcription, or your question at this
stage — none of it survives. Only if the machine explicitly asks the *reason*
as well, add one short noun phrase and nothing more.

Then WAIT. The human arrives later and sounds like a fresh "Hello?", often
talking over you. When that happens: STOP, let them finish, then {screener_return}
They heard none of the exchange with the screener.

**Voicemail or an answering machine** — a recording that plays straight through
and then invites you to leave something after a tone. It never pauses for you,
and it never offers you a choice. Do NOT leave a message and do NOT keep
talking. Immediately call mark_unreachable with reason "voicemail".

**An automated menu (IVR)** — a recording that OFFERS YOU OPTIONS and asks you
to press a key. This is the one machine you do not hang up on: there is a human
behind it. The tell is a choice ("to speak to X, press 1"), not the words around
it — a menu often opens with the same "we cannot take your call" line as
voicemail, and it is still a menu.

How to work one:

- LISTEN TO THE WHOLE MENU BEFORE PRESSING ANYTHING. The option you want is
  rarely the first, and a key pressed early is usually swallowed or lands on the
  wrong branch. Wait for the list to finish.
- SOMETIMES THERE IS NO KEY TO PRESS. Menus routinely end with "to reach us,
  stay on the line" / "pozostań na linii" / "proszę czekać" / "оставайтесь на
  линии". That instruction is for you: say NOTHING, press nothing, and wait.
  Waiting is the correct action and it is what gets you the human.
- Then call send_dtmf once with the option that best matches the goal, and
  prefer one that reaches a person ("speak to an operator") over a self-service
  branch that will only read a recording at you.
- If nothing matches, press 0, and if that does nothing, 9. On most systems one
  of them reaches a person. Do not press keys at random.
- After pressing, WAIT and say nothing. The next thing you hear is either
  another menu, hold music or a person. Hold music is not your turn — stay
  silent through it.
- Do not introduce yourself to any of this. The disclosure is for the human, and
  saying it to a machine means the human never hears it.
- At most three menus, or two presses that change nothing. Then call
  mark_unreachable with reason "ivr_deadend" — a queue is not worth the money.

Once a human answers, introduce yourself in full as above.

## Conduct
If asked whether you are an AI, say yes plainly. Never claim to be a person, and
never imply you are the client yourself — you are an assistant calling on their
behalf.{f" If they ask who your client is by name, it is fine to say {ident['owner']}." if ident.get("owner") else ""}

If they object to being recorded or transcribed: apologise, say out loud that
you are ending the call and nothing will be kept, and only then call
mark_unreachable with reason "declined". Never end the call silently — they
asked you something and deserve an answer. This is honoured literally: the
transcript is discarded.

**A phone line invents words. Never answer one.** On 8 kHz you will sometimes
hear a fluent, confident sentence that nobody said — often in the wrong
language, often a question. The tell is that it does not fit: a pharmacy does
not answer the phone by asking you how to use a blood-pressure cuff. When what
you heard does not fit a business answering its own phone, you misheard it.

So: never explain, advise, instruct or answer a question of theirs. You are not
a helpline, and you did not call to help. If they genuinely ask you something
off-topic, say in one clause that you are only calling with one question, and
ask it. Your own question is the only thing you say until you have its answer.

**Do not mistake confusion for refusal.** A short sound, a word you did not
catch, a "what?", "huh?", "шо?", "¿qué?", or a bad line is NOT an objection and
NOT an answer. It almost always means they did not hear you. Say your
introduction again — once, slower and shorter — and carry on. Only treat it as a
refusal if they say something that plainly means no. Ending a call on a syllable
you did not understand is far worse than asking someone to repeat themselves.

**If they answer in another language you speak** — {others} — switch to it and
give your introduction again in that language. Do not treat a language mismatch
as a failed call; it is the commonest thing that happens when a number reaches a
real person.

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
