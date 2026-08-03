"""Every clock the agent runs on, in one place.

These are separated out because they are not independent: conflating them is
what made the agent simultaneously jumpy and slow, and the failure mode of each
one is a different kind of expensive. A number changed here without reading its
neighbours is how a fix to one silence re-breaks another.

Four different silences, four different meanings:

    nobody has spoken yet          → probe (OPENING_SILENCE_SECONDS)
    they spoke and stopped         → is the turn over? (END_OF_TURN_SILENCE_MS)
    dead air mid-call              → nudge, don't hang (SILENCE_NUDGE_SECONDS)
    a menu told us to hold         → wait, say nothing (QUEUE_PATIENCE_*)

and two budgets that bound the whole call regardless of which silence it is in.
"""

from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger("zvonok.timing")

# --- the four silences ------------------------------------------------------

# Nobody said anything after pickup → we probe (NOT introduce: introducing here
# hard-codes the "a person answered" branch before we have heard who answered,
# which is how a real answering machine once got a polite introduction).
#
# 3.0, not 1.5: a real callee's "hello" has to cross the PSTN leg, the SIP media
# path and VAD before we see it. At 1.5 s we repeatedly decided the line was
# silent while the person had in fact already spoken, and talked over them.
OPENING_SILENCE_SECONDS = 3.0

# They said something and stopped → how long before assuming the turn is over.
# Must be generous: a screener says "Hello?" and only THEN "please state your
# name after the tone". Answering the bare "Hello?" means missing the actual
# request. OpenAI's server_vad default is 500 ms, which is too eager on a phone.
#
# ⚠ This is also a LATENCY FLOOR. The callee's experienced pause is this plus
# the model's time-to-first-audio plus egress, so it is the first number to
# reach for when a call feels slow — and the first one to break turn-taking if
# it is lowered without measuring.
END_OF_TURN_SILENCE_MS = 800

# Mid-call dead air after someone has already spoken → nudge, don't hang.
SILENCE_NUDGE_SECONDS = 6.0
MAX_SILENCE_NUDGES = 2

# …unless a menu has just told us to hold. Then quiet is the queue doing its job
# and both the nudge and the hang-up are wrong. 60 s is about as long as a
# pharmacy switchboard is worth waiting for.
QUEUE_PATIENCE_SECONDS = 60.0
# A hard ceiling on the whole wait, because the above is refreshed by every menu
# turn and a switchboard that loops its announcement would extend it forever.
# Measured from when we FIRST started waiting, not from the last menu.
QUEUE_PATIENCE_TOTAL = 90.0

# --- the two budgets --------------------------------------------------------

# Nothing INTELLIGIBLE from the far end in this long. Distinct from "nothing at
# all" below: hold music, a fax tone and a noisy open line all satisfy VAD
# indefinitely while saying nothing, so VAD energy cannot bound them.
NO_SPEECH_BUDGET_SECONDS = 75.0

# Absolute budget for a line where the callee has NEVER made a sound, measured
# from answer. A person who is merely distracted trips the nudge cycle; a line
# that answered with no media has nobody to nudge. Carriers do return 200 OK and
# then deliver no audio at all — observed, and it billed for the full duration.
NO_RESPONSE_BUDGET_SECONDS = 20.0

# --- call bounds ------------------------------------------------------------

DEFAULT_MAX_DURATION = 300
HARD_MAX_DURATION = 600  # never exceeded regardless of metadata (BRIEF §6)
RINGING_TIMEOUT_SECONDS = 45

# --- audio ------------------------------------------------------------------

# How eagerly the callee's audio counts as speech, and therefore as a barge-in.
# Raised from the 0.5 default because PSTN line noise was interrupting the agent
# mid-sentence, producing "speech not done in time after interruption" storms.
# Barge-in was misdiagnosed as an end-of-turn problem; it is interrupt hysteresis.
BARGE_IN_THRESHOLD = 0.65

# Linear gain on the agent's outgoing speech, applied to the realtime model's PCM
# before it hits the G.711 leg — the only point in the path where we control the
# level at all. Callees consistently reported the agent as quiet-but-intelligible;
# 1.4 is about +3 dB.


def output_gain() -> float:
    """Parse ZVONOK_OUTPUT_GAIN defensively.

    A typo in one env var must not take the whole calling capability down at
    import time, and 0/NaN would silently mute every call — worse than loud.
    Clamped to [0.1, 4.0]: beyond ~4x everything clips into noise anyway.
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
