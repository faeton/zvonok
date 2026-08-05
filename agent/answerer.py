"""Who or what picked up, judged from what was said.

Four things answer a phone and they need opposite behaviour: a person gets an
introduction, voicemail gets an immediate hangup, an IVR gets a keypress and
then silence, a screener gets four words. Getting this wrong is expensive in
both directions — introducing ourselves to a recording pays to be talked at,
hanging up on a switchboard throws away a call that was about to connect.

This module is deliberately free of every dependency. It is phrase matching over
text, it is the part most likely to need a new entry after a call goes wrong, and
it should be editable and testable without the LiveKit runtime anywhere near it.

⚠ It is a SAFETY NET under the prompt, not the primary mechanism. The prompt
judges by the SHAPE of the turn — a person says something short and yields, a
machine talks through a script and never yields — which generalises to languages
and conventions no list here will ever cover. These phrases exist because the
prompt alone demonstrably failed to hang up on a real answering machine.
"""

from __future__ import annotations

import re

# Voicemail greetings, narrowly matched.
VOICEMAIL_PHRASES = (
    "leave a message", "leave your message", "leave your name", "after the beep",
    "at the tone", "is not available", "not available right now", "voicemail",
    "voice mail", "record your message", "unable to take your call",
    # A real greeting we hit said "leave your email so I can write you back" —
    # never the word "message". Phrase matching is inherently whack-a-mole; the
    # real fix is not introducing ourselves before we have heard who answered.
    "leave your email", "write you back", "better to reach us",
    "оставьте сообщение", "оставьте свое сообщение", "оставьте своё сообщение",
    "после сигнала", "автоответчик", "не может ответить", "недоступен",
    "deje su mensaje", "deje un mensaje", "después de la señal", "buzón de voz",
    "no está disponible",
    "zostaw wiadomość", "zostaw wiadomosc", "nagraj wiadomość", "nagrać wiadomość",
    "po sygnale", "poczta głosowa", "poczta glosowa", "automatyczna sekretarka",
    "nie mogę teraz odebrać", "abonent jest niedostępny", "abonent czasowo niedostępny",
)

# A call screener ALSO says "after the tone" — but it asks for your name and says
# it will connect you. Misreading a screener as voicemail hangs up on a call that
# was about to reach a human, so these veto the match.
SCREENER_PHRASES = (
    "state your name", "say your name", "who's calling", "who is calling",
    "connect you", "screening", "reduce spam",
    "kto dzwoni", "proszę się przedstawić",
)

# So does a menu, and for the same reason: a pharmacy switchboard that opens
# "we cannot take your call right now — to speak to the pharmacy press 1" is an
# IVR with a human behind it, not voicemail.
MENU_PHRASES = (
    "press", "dial", "for more options", "main menu",
    "нажмите", "наберите", "добавочный",
    "marque", "pulse",
    "naciśnij", "nacisnąć", "wciśnij", "wybierz tonowo", "wybierz numer",
    "aby połączyć", "aby uzyskać", "aby porozmawiać", "wybierz jeden",
)

# A recording that offers no options and invites no message, but tells you to
# wait. It is neither a menu nor voicemail, and until a Figueres pharmacy proved
# it we had no category for it: the agent heard a machine reading its opening
# hours, took it for the person who had answered, and spent its whole question
# on a tape. Fifteen seconds later a human said "digui" and got a second
# introduction that collided with them, and the call died in the overlap.
#
#   "Està trucant a Farmàcia Soler. El nostre horari és de dilluns a dissabte…
#    En breus moments l'atendrem. Gràcies."
#
# Catalan, because these are Catalan pharmacies — Figueres, Girona, Barcelona.
# We keep speaking Spanish there (everyone understands it), but we have to
# RECOGNISE what the machine says, and this list is the only place that matters.
# The Spanish forms are here too: the same recording exists in both.
HOLD_PHRASES = (
    # ca
    "en breus moments", "l'atendrem", "atendrem de seguida", "esperi un moment",
    "un moment si us plau", "el nostre horari", "no pengi",
    # es
    "en breves momentos", "le atenderemos", "les atenderemos",
    "espere un momento", "un momento por favor", "nuestro horario",
    "no cuelgue", "su llamada es importante",
    # en / ru / pl, for the same recording elsewhere
    "please hold", "hold the line", "be with you shortly", "our opening hours",
    "оставайтесь на линии", "ожидайте ответа", "наш режим работы",
    "prosimy czekać", "prosimy czekac", "godziny otwarcia",
)

# Matched on WORD BOUNDARIES, not as substrings, and the trailing \b is the half
# that does the work: "pressure" starts on a word boundary too. A plain
# `"press" in text` classified the ASR hallucination "How to take a pressure?"
# as an IVR menu and bought that call sixty seconds of patient silence — on a
# line where nobody had said anything of the sort.
#
# HOLD_PHRASES ride the same regex because they drive the same behaviour: both
# mean "a machine is talking, stay silent, a human is coming". agent.py's log
# line has said "menu or hold detected" all along; only the hold half was
# missing.
_MENU_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(p) for p in MENU_PHRASES + HOLD_PHRASES),
    re.IGNORECASE,
)

# Hold music is not silence to a recogniser: it produces a steady drip of short,
# confident, usually English fragments — "You", "Thank you", "Bye" — the classic
# hallucinations an ASR emits when asked to transcribe non-speech. Each one looks
# like the callee talking: it pads the transcript the extractor reads, and it
# invites the model to answer a person who is not there.
#
# Matched only as a WHOLE short turn. A real "tak" or "nie" is exactly this short
# and must survive — dropping a one-word answer would discard the entire point of
# a canvassing call.
_NOISE_TURNS = frozenset({
    "you", "thank you", "thanks", "thank you.", "bye", "bye.", "okay", "ok",
    ".", "..", "...", "?", "!", "-", "the", "so", "um", "uh",
})
_NOISE_STRIPPED = frozenset(t.strip(".,!?-–— ") for t in _NOISE_TURNS)


def looks_like_menu(text: str) -> bool:
    return bool(_MENU_RE.search(text))


def is_noise_turn(text: str) -> bool:
    """A transcript fragment that no human produced.

    ⚠ Dropping these does NOT stop hold music from keeping a call alive. VAD sets
    the activity clocks from audio ENERGY, a second or more before any transcript
    exists, so by the time this runs the watchdog has already been reset. What
    bounds a queue is the no-intelligible-speech budget, not this function. This
    only keeps the extractor from reading hallucinations as answers.
    """
    return text.strip().lower().strip(".,!?-–— ") in _NOISE_STRIPPED


def looks_like_voicemail(text: str) -> bool:
    low = text.lower()
    if any(s in low for s in SCREENER_PHRASES):
        return False
    if looks_like_menu(low):
        return False
    return any(p in low for p in VOICEMAIL_PHRASES)
