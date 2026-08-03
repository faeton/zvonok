"""The voice brain: which model, and what its ear is pointed at.

Small module, one important function, and the important function exists because
of a bug that was invisible for an entire canvass. Read `build_realtime_model`
before changing anything here.
"""

from __future__ import annotations

import logging
import os

import numpy as np
from livekit import rtc
from livekit.plugins import xai
from openai.types.beta.realtime.session import TurnDetection
from openai.types.realtime import AudioTranscription

from timing import BARGE_IN_THRESHOLD, END_OF_TURN_SILENCE_MS

logger = logging.getLogger("zvonok.voice")

# Grok Voice. The plugin still defaults to think-fast-1.0; 2.0 (announced
# 2026-07-29) claims 1.5–2x better transcription WER across 24 languages and a
# far larger margin on noisy telephony — which is the entire problem on an 8 kHz
# G.711 leg. `grok-voice-latest` only flips to 2.0 on 2026-08-05, and an alias
# that changes under a running deployment is not something to discover from a
# transcript, so the id is pinned explicitly.
GROK_VOICE_MODEL = os.getenv("ZVONOK_VOICE_MODEL", "grok-voice-think-fast-2.0")


def apply_gain(frame: rtc.AudioFrame, gain: float) -> rtc.AudioFrame:
    """Boost the model's PCM before it is encoded to G.711.

    Samples are clamped to int16, so an over-eager env override degrades to
    distortion rather than to a crash.
    """
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


def transcription_options(
    language: str, keywords: list[str] | None = None
) -> AudioTranscription:
    """The ASR hint, in the field names xAI actually reads.

    ⚠ THIS IS WHERE A WHOLE CANVASS WAS LOST, TWICE, IN TWO DIFFERENT WAYS.

    First failure: `livekit-plugins-xai` hardcodes `AudioTranscription()` — every
    field None, which means AUTOMATIC LANGUAGE DETECTION. On the first Polish
    calls that broke exactly where detection is weakest: the first second. A long
    IVR announcement came back verbatim, while a two-word "Apteka X, słucham?"
    came back as the English sentence "I think I look to the." One such
    mishearing ("How to take a pressure?") the model then ANSWERED, giving a
    pharmacy instructions on a blood-pressure cuff instead of asking about the
    drug. A misheard greeting is not a transcript defect — it redirects the call.

    Second failure, and the reason this function exists rather than a one-line
    assignment: the OBVIOUS fix was wrong and looked right. `AudioTranscription`
    is an OpenAI type, so it offers `language` and `keywords`. **xAI reads
    neither.** Its fields are `language_hint` (BCP-47) and `keyterms` (max 100
    terms, 50 chars each). The realtime server accepts and ECHOES BACK any keys
    you send it — verified against a live session, including deliberate nonsense
    — so the wrong names produced no error, no warning, and a `session.updated`
    that looked like confirmation. Auto-detect stayed on for every call and the
    drug names never reached the recogniser.

    The lesson worth keeping: an echo is not an acknowledgement. This provider
    speaks the OpenAI realtime PROTOCOL, which is not the same as accepting the
    OpenAI realtime SCHEMA, and the SDK's type will happily carry either because
    it permits extra fields.

    `model` is deliberately NOT set. Setting it to "grok-transcribe" switches the
    server to `conversation.item.input_audio_transcription.updated`, which the
    xAI plugin does not handle — it only handles `.completed` (distinguishing
    partials by a `status` field). We would lose every user transcript, which is
    a worse failure than the one this function fixes.
    """
    return AudioTranscription(
        # BCP-47. Our language codes are already valid BCP-47 primary subtags.
        language_hint=language,
        # Proper nouns are what a canvass turns on — a drug, a part number, a
        # brand — and they are the first thing an 8 kHz line destroys. Supplied
        # per call by the requester, who is the only one who knows them.
        keyterms=list(keywords) if keywords else None,
    )


def build_realtime_model(
    language: str, keywords: list[str] | None = None
) -> xai.realtime.RealtimeModel:
    """The voice brain, with its ear pointed at the language we are calling in.

    The plugin does not expose `input_audio_transcription`, so we set it on the
    options object it built. Ugly, and deliberately preferred over dropping to
    the parent `openai.realtime.RealtimeModel` with an x.ai base_url (which
    BRIEF §5.3 sanctions): the xAI subclass carries Grok-specific event handling
    — notably partial-transcript disambiguation — that we would silently lose.
    """
    model = xai.realtime.RealtimeModel(
        model=GROK_VOICE_MODEL,
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
    )
    transcription = transcription_options(language, keywords)
    model._opts.input_audio_transcription = transcription
    # Log what will actually go on the wire, not what we meant to send. The
    # previous version of this line reported "ASR pinned to pl" while sending a
    # field the server ignored, and that log was read as evidence for months.
    logger.info(
        "voice model %s, transcription options %s",
        GROK_VOICE_MODEL, transcription.model_dump(exclude_none=True),
    )
    return model
