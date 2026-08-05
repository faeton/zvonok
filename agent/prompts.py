"""Everything the agent says, and everything that checks it said it.

Two kinds of text live here and they are not the same kind of thing:

  FIXED TEXT   — the disclosure and the probe. Spoken as close to verbatim as a
                 speech-to-speech model allows, because both were demonstrably
                 mangled when left to the model. The disclosure is a legal
                 requirement (§8); it cannot depend on winning a race against an
                 impatient human and line noise.
  INSTRUCTIONS — the system prompt. Judgment, in prose, for a model to apply.

The module is text, `os.getenv` and the standard library. It imports nothing from
LiveKit or the realtime stack: this is the part that changes after every call
that goes wrong, and it must be testable in a bare interpreter.

The owner's personal details come EXCLUSIVELY from the environment (configured
on the deploy host, never in this repository — the same rule as secrets). Unset
values simply drop out of the prompt: an unconfigured deployment degrades to an
agent that promises a confirmation, never one that invents a name.
"""

from __future__ import annotations

import os
import re
import unicodedata
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
        # by default; the delivery check folds w and v together, so either
        # transcription satisfies it.
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
#
# Checked against THE LINE WE ASKED FOR, clause by clause — not against
# dictionaries of words meaning "AI" and "kept" in four languages. Those
# dictionaries were wrong in two ways that only surfaced when someone went
# looking, and both were silent:
#
#   · They were never scoped to the call's language. Every language's words were
#     accepted for every call, so at `brief` — where no name is required — a
#     disclosure in any of the four satisfied a call in any other.
#   · They could not tell `full` from `light`. Both levels were checked with the
#     same two lists, so a light introduction, which never says the call is
#     STORED and never offers to stop, closed the guard on a call designated
#     `full` precisely because it commits on someone's behalf.
#
# Matching the known line fixes both, and turns the Klava/Klawa spelling hack
# into ordinary normalisation. What this must NOT become is one similarity score
# over the whole sentence: the disclosure carries two separable facts — you are
# talking to an AI, and what you say is kept — and dropping the retention clause
# costs a small fraction of the words but half of §8. So the unit is the CLAUSE,
# and every clause of the expected line has to land in the same breath.
#
# The bias is deliberate. A false negative makes the guard say the line a second
# time, which is rude. A false positive records a §8 disclosure that never
# happened. When the threshold is in doubt, be rude.

# Share of a clause's words that must show up for it to count as spoken. Not
# 1.0: an 8 kHz alaw line reliably eats a word or two of anything this long.
_CLAUSE_RECALL = 0.75

_SENTENCE_END = re.compile(r"[.!?…]+")


def _fold(text: str) -> list[str]:
    """Lowercase, drop accents and punctuation, fold w→v, split into words.

    Both sides of every comparison go through this, so each fold can only ever
    remove a distinction — never invent a match the raw strings could not
    support. The w→v fold lives here rather than in a per-name variants table
    because it is an artefact of the recogniser, not a fact about the name:
    Polish has no native "v", so a spoken "Klawa" comes back transcribed either
    way, and so does every other foreign word on the call.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower().replace("w", "v"))
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c if c.isalnum() else " " for c in bare).split()


def _heard(word: str, said: list[str]) -> bool:
    """Is this word in what was transcribed, allowing for a mangled ending?

    A phone line degrades endings long before it degrades stems, and every
    language here except English inflects heavily, so "zanotuję" / "zanotuje" /
    "zanotowałam" have to count for each other. Prefix matching is capped below
    four characters, where it would start matching function words to each other.
    """
    if word in said:
        return True
    if len(word) < 4:
        return False
    return any(
        len(s) >= 4 and (s.startswith(word) or word.startswith(s)) for s in said
    )


def _clauses(line: str) -> list[list[str]]:
    """The expected line, split into the units that carry a fact each."""
    parts = (_fold(c) for c in _SENTENCE_END.split(line))
    return [p for p in parts if len(p) >= 2]


def _spoke_clause(clause: list[str], said: list[str]) -> bool:
    return sum(1 for w in clause if _heard(w, said)) / len(clause) >= _CLAUSE_RECALL


# The retention fact, as stems, because word recall alone cannot protect it.
#
# ⚠ WHY THIS EXISTS. `_spoke_clause` scores a clause by the SHARE of its words
# that came back, which spreads the weight evenly over "здравствуйте", "это",
# "AI-ассистент" and "запишу". The three §8 levels are not equally exposed to
# that: `full` puts retention in a sentence of its own, so losing it fails the
# clause outright, but `brief` is ONE sentence in every language, and `light`'s
# retention half is short. Measured against the real templates, dropping the
# storage verb alone still cleared 0.75 in ru, pl and es:
#
#   "Здравствуйте, это AI-ассистент, ответ."      → 5 of 6 words → counted
#   "Dzień dobry, tu asystent AI, odpowiedź."     → counted
#   "Hola, soy un asistente de IA, la respuesta." → counted
#
# In each the callee was told a machine was calling and never told the answer is
# kept — half of §8 missing, and the guard satisfied, so it would never make the
# agent say it again. English survived only by being wordier, which is luck, not
# design. So the fact gets its own requirement: whatever else matched, at least
# one of these stems has to have been heard in that same turn.
#
# Stems, not words, because `_heard` prefix-matches and every language here
# inflects the verb ("запишу" / "записываю", "zanotuję" / "zapisywana").
_RETENTION_STEMS = {
    "en": ("note", "noting", "transcrib", "stored", "storing", "record"),
    "ru": ("запиш", "записыва", "транскрибир", "сохран", "храни"),
    "es": ("anotar", "anoto", "transcrib", "guard"),
    "pl": ("zanotuj", "notuj", "zapis", "transkryb"),
}


def _kept_the_retention_fact(language: str, said: list[str]) -> bool:
    """Did this turn actually say the answer is kept?

    Falls back to English rather than to "yes": an unknown language must not buy
    a pass on the half of §8 that is about retention.
    """
    stems = _RETENTION_STEMS.get(language, _RETENTION_STEMS["en"])
    return any(_heard(s, said) for stem in stems for s in _fold(stem))


def disclosure_delivered(
    turns: list[dict[str, Any]],
    language: str,
    level: str = "light",
    introduce_as: str | None = None,
    also_languages: tuple[str, ...] = (),
) -> bool:
    """Did a complete disclosure actually get spoken, in one piece?

    Checked against what the agent SAID, not what it intended: every clause of
    `disclosure_for(language, level, introduce_as)` has to land inside a SINGLE
    uninterrupted assistant turn, and at `light` and `full` the assistant's name
    has to be in that same turn.

    ⚠ The `interrupted` check is the part that makes this true rather than merely
    claimed. Without it the function only matched text, so an introduction cut
    off mid-sentence still counted as delivered, because the stored turn text
    contains words the callee never heard.

    Pass the call's `introduce_as`. The guard compares against the exact line it
    would force, and a custom principal that the checker does not know about
    reads as a clause that never landed.

    A disclosure STRONGER than the one asked for also counts; a weaker one never
    does. `full` is not a superset of `light`'s wording, only of its meaning, so
    the levels at or above the requested one are each tried in full.

    The owner is deliberately NOT required at any level: the introduction names a
    role ("assistant of a potential client"), not a person. At `brief` the
    assistant's own name is not required either, because the brief line does not
    contain one — demanding it here while removing it from what the agent speaks
    would leave the guard convinced the disclosure never landed, and it would cut
    across the callee to say it again.

    `also_languages` covers the mid-call switch: the conversational prompt tells
    the agent to change language when the callee plainly answers in another one,
    and to give its introduction again there. Without it the guard would judge a
    Polish call by its Russian line and talk over someone who had just heard the
    disclosure. Note what this does NOT re-open — a switched-language disclosure
    counts only as a COMPLETE line in that language, never as a stray word from
    it. A custom `introduce_as` is not carried across, because the model would
    have translated it and we cannot; an unusual principal costs one re-say.
    """
    langs = [(language, introduce_as)]
    langs += [(lang, None) for lang in also_languages if lang != language]
    # The levels are ordered by strength, and a stronger disclosure always
    # satisfies a weaker requirement — `full` says everything `light` says and
    # then some. It has to be spelled out because the levels are not each other's
    # substrings: `full` says the call is "transcribed and stored" where `light`
    # says "I'll note down your answer", so matching the requested wording alone
    # would send the guard in to re-say the shorter line over a callee who had
    # just heard the longer one.
    try:
        levels = DISCLOSURE_LEVELS[DISCLOSURE_LEVELS.index(level):]
    except ValueError:  # unknown level — hold it to the default
        levels = DISCLOSURE_LEVELS[DISCLOSURE_LEVELS.index("light"):]
    expected = [
        (lvl, lang, _clauses(disclosure_for(lang, lvl, intro)),
         identity_for(lang)["assistant"])
        for lang, intro in langs
        for lvl in levels
    ]

    for t in turns:
        if t["speaker"] != "assistant" or t.get("interrupted"):
            continue
        said = _fold(t["text"])
        for lvl, lang, clauses, name in expected:
            if lvl != "brief" and not all(_heard(w, said) for w in _fold(name)):
                continue
            # Both halves of §8, separately. Clause recall alone lets the short
            # templates lose the retention verb and still pass — see
            # _RETENTION_STEMS for the three languages that measurably did.
            if not _kept_the_retention_fact(lang, said):
                continue
            if clauses and all(_spoke_clause(c, said) for c in clauses):
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
    prior_attempt: str | None = None,
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
    # Empty for almost every call, and deliberately rendered as nothing at all
    # rather than an empty heading the model might try to fill.
    already = f"\n## You have rung them already\n{prior_attempt}\n" if prior_attempt else ""

    return f"""You are an {ident['role']} making a short phone call for a client. A couple of questions, then you hang up. Under a minute.
{already}

## Your question
{goal}

This is written for you, not to be read out. Ask it in your own words, in the second person, one thing at a time.

If the goal asks several things, they are ordered: ask the first, wait for the answer, then ask the next only if that answer makes it worth asking. Never stack two questions into one breath.
{facts_block(answer_schema, brief=True)}
## What you say first
The moment a HUMAN speaks, say this and only this, in one breath:
"{disclosure_for(language, disclosure_level)} <your question>"

This first turn is fixed. Whatever you think you heard, you say this. Do not react to their greeting, do not comment on it, and never answer it.

## What you never do
You are not a helpline. Never explain, advise, instruct, or answer a question of theirs. On an 8 kHz line you will sometimes hear a fluent sentence nobody said — often in another language, often a question. The tell is that it does not fit: a business does not answer its own phone by asking YOU for advice. When it does not fit, you misheard, so ask your question again instead of replying to it.

No small talk, no offering help, no apologising for calling, no asking the same thing twice in different words. Do not give your name unless asked (it is {ident['assistant']}).

## Are they even the right place
If the goal names the business you were told to call, and whoever answers names a plainly different one, you have the wrong number: say sorry, you were calling <business>, then mark_unreachable "wrong_number". Do not ask your question anyway. An 8 kHz line mangles business names, so a name you merely did not catch is not a mismatch — if you are unsure, ask once whether you have reached them, and believe their answer.

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

Your opening is deliberately short. If they ask who is calling, who you are calling for, or what happens to what they say, THEN give the longer version: that you are {ident['assistant']}, an AI assistant calling for a client, and that the call is transcribed and stored. Save it for someone who asked — said up front to someone who did not, it costs the seconds the call needs.

## Finishing
When the goal's questions are answered — or the first answer makes the rest pointless, which "no, we don't have it" usually does — read the facts back, call record_answer, thank them in three words, call end_call. Do not keep a person on the line for a question whose answer you can already infer. A fifteen-second "no" is a success."""


def build_instructions(
    goal: str,
    language: str,
    *,
    s2s: bool = True,
    disclosure_level: str = "light",
    answer_schema: dict[str, Any] | None = None,
    introduce_as: str | None = None,
    prior_attempt: str | None = None,
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
            prior_attempt=prior_attempt,
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
    # Same block as the canvass template, same reason: rendered as nothing at
    # all when there is no earlier call, so the model is never shown an empty
    # heading and never invents a previous conversation to fill it.
    already = f"\n## You have rung them already\n{prior_attempt}\n" if prior_attempt else ""

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
{already}
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
