#!/usr/bin/env python3
"""Does xAI's `language_hint` actually change what comes back? (BRIEF §10 item 2)

The plumbing question is settled: the options we build serialise to
`session.audio.input.transcription = {"language_hint": ..., "keyterms": [...]}`
in xAI's field names. What is NOT settled is whether the server acts on them,
and it cannot be settled from the protocol — the schema is not validated
server-side and unknown keys are echoed back unchanged (BRIEF §5.3.2). That
echo is what let the wrong field names survive an entire canvass (§9.4).

So: same audio, hint on vs hint off, transcripts compared. This talks to the
realtime API directly rather than through LiveKit, because the thing under test
is the server's behaviour and every layer in between is a place for a
difference to be introduced or hidden.

    python3 asr_probe.py call.wav --arm es          # language_hint="es"
    python3 asr_probe.py call.wav --arm off         # no hint at all
    python3 asr_probe.py call.wav --arm es --keyterms "Ozempic,semaglutyd"

Input must be mono PCM WAV; any rate (it is resampled to the API's 24 kHz).
Feed it 8 kHz telephone audio — a hint that changes nothing on clean studio
speech tells us nothing, because auto-detect gets that right anyway. The whole
question is what happens on the degraded first second that decides a call.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import json
import os
import sys
import time
import wave

try:
    import websockets
except ImportError:  # pragma: no cover - environment problem, not a code path
    sys.exit("needs `websockets` — run this inside the agent image, which has it")

URL = os.getenv("XAI_REALTIME_URL", "wss://api.x.ai/v1/realtime")
MODEL = os.getenv("ZVONOK_VOICE_MODEL", "grok-voice-think-fast-2.0")
API_RATE = 24000  # what the realtime API expects, per the OpenAI-compatible schema
CHUNK_MS = 100


def load_pcm(path: str, start: float, dur: float) -> tuple[array.array, int]:
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: need mono 16-bit PCM")
        rate = w.getframerate()
        pcm = array.array("h")
        pcm.frombytes(w.readframes(w.getnframes()))
    # Windowing is the point, not a convenience. Language ID is weakest on a
    # short opening utterance — which is the second that decides a call (§9.4)
    # — and a 60 s clip of unambiguous speech has no power to detect a hint at
    # all. Feed it the first second of the greeting, not the whole recording.
    if start:
        pcm = pcm[int(start * rate):]
    if dur:
        pcm = pcm[:int(dur * rate)]
    return pcm, rate


def noisify(pcm: array.array, snr_db: float) -> array.array:
    """Additive white noise at a chosen SNR. Degradation is how the probe gets
    power: a hint can only show up where auto-detect is unsure."""
    if snr_db >= 90:
        return pcm
    n = len(pcm)
    sig = (sum(x * x for x in pcm) / n) ** 0.5 if n else 0.0
    amp = sig / (10 ** (snr_db / 20.0))
    # Deterministic LCG: an experiment whose noise differs per arm is not an
    # experiment. Same seed => byte-identical noise across arms.
    seed = 0x2545F491
    out = array.array("h", bytes(2 * n))
    for i in range(n):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        v = int(pcm[i] + amp * ((seed / 0x3FFFFFFF) - 1.0))
        out[i] = max(-32768, min(32767, v))
    return out


def resample(pcm: array.array, src: int, dst: int) -> array.array:
    """Linear interpolation. Deliberately not audioop: it was removed in 3.13,
    and this runs wherever the agent image happens to be."""
    if src == dst:
        return pcm
    n = len(pcm)
    out = array.array("h", bytes(2 * int(n * dst / src)))
    step = src / dst
    for i in range(len(out)):
        pos = i * step
        j = int(pos)
        if j + 1 >= n:
            out[i] = pcm[n - 1]
            continue
        frac = pos - j
        out[i] = int(pcm[j] * (1.0 - frac) + pcm[j + 1] * frac)
    return out


def transcription_options(arm: str, keyterms: list[str] | None,
                          field: str, kw_field: str) -> dict:
    """The exact shape agent/voice.py puts on the wire — xAI's field names, and
    `model` deliberately unset (setting it switches the server to `.updated`
    events the plugin cannot handle).

    `field` is settable so the OpenAI spelling (`language`) can be sent as a
    negative control. That is the whole experiment: the right name must move the
    transcript and the wrong name must not, or §9.4's root cause is still a
    story rather than a finding."""
    opts: dict = {}
    if arm != "off":
        opts[field] = arm
    if keyterms:
        opts[kw_field] = keyterms
    return opts


async def run(path: str, arm: str, keyterms: list[str] | None, pace: float,
              field: str, kw_field: str, start: float, dur: float,
              snr_db: float, pad: float) -> None:
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise SystemExit("XAI_API_KEY not set — this is meant to run on the deploy host")

    pcm, src_rate = load_pcm(path, start, dur)
    pcm = noisify(pcm, snr_db)
    pcm = resample(pcm, src_rate, API_RATE)
    total_s = len(pcm) / API_RATE
    opts = transcription_options(arm, keyterms, field, kw_field)

    print(f"arm:            {arm}")
    print(f"transcription:  {json.dumps(opts)}")
    print(f"audio:          {path}  {src_rate} Hz -> {API_RATE} Hz, "
          f"{total_s:.1f}s  @{start:.1f}s  snr={snr_db}dB")
    print()

    async with websockets.connect(
        f"{URL}?model={MODEL}",
        additional_headers={"Authorization": f"Bearer {key}"},
        max_size=None,
    ) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "session.created":
            raise SystemExit(f"unexpected first frame: {hello}")

        # create_response=false: we want the recogniser, not a conversation.
        # If xAI ignores it we simply discard the audio deltas below.
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": API_RATE},
                        "transcription": opts,
                        "turn_detection": {
                            "type": "server_vad",
                            "silence_duration_ms": 800,
                            "create_response": False,
                        },
                    }
                },
            },
        }))

        applied = asyncio.Event()
        errors: list[dict] = []
        lines: list[str] = []

        async def reader() -> None:
            async for raw in ws:
                ev = json.loads(raw)
                kind = ev.get("type", "")
                if kind == "session.updated":
                    echoed = (
                        ev.get("session", {})
                        .get("audio", {})
                        .get("input", {})
                        .get("transcription")
                    )
                    # Printed, never trusted: the server echoes unknown keys, so
                    # this frame agreeing with us is what §9.4 mistook for proof.
                    # It IS trusted for one thing only — ordering. Streaming
                    # audio before the update is acknowledged would let the
                    # decisive first second be decoded under defaults, and the
                    # arm would look like a no-effect result either way.
                    print(f"[echo] {json.dumps(echoed)}\n")
                    applied.set()
                elif kind.startswith("conversation.item.input_audio_transcription"):
                    text = (ev.get("transcript") or "").strip()
                    if not text:
                        continue
                    status = ev.get("status", "")
                    if kind.endswith(".completed") and status != "in_progress":
                        lines.append(text)
                        print(f"  {len(lines):>2}. {text}")
                elif kind == "error":
                    # Recorded, not just printed. A silently rejected
                    # session.update is indistinguishable from a field that
                    # does nothing — the exact confusion this probe exists to
                    # resolve — so an arm that errored must not be scored.
                    errors.append(ev.get("error") or {})
                    print(f"[error] {json.dumps(ev.get('error'))}", file=sys.stderr)
                    applied.set()

        rx = asyncio.create_task(reader())
        try:
            await asyncio.wait_for(applied.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            raise SystemExit("no session.updated before timeout — arm not scorable")

        chunk = int(API_RATE * CHUNK_MS / 1000)
        t0 = time.monotonic()
        for i in range(0, len(pcm), chunk):
            buf = pcm[i:i + chunk].tobytes()
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(buf).decode(),
            }))
            # Paced, not blasted: server VAD segments on timing, and a 60 s file
            # delivered in two seconds is not the call we are trying to model.
            target = (i + chunk) / API_RATE / pace
            drift = target - (time.monotonic() - t0)
            if drift > 0:
                await asyncio.sleep(drift)

        # Send real silence, do not merely stop sending. Server VAD closes a
        # turn on a silence WINDOW in the audio it receives; a clip that ends
        # mid-word and then goes quiet on the wire never produces an
        # end-of-turn, so the segment is never committed and the arm scores
        # zero regardless of what the recogniser heard. That is a probe bug
        # that looks exactly like a null result.
        quiet = array.array("h", bytes(2 * chunk)).tobytes()
        for _ in range(int(pad * 1000 / CHUNK_MS)):
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(quiet).decode(),
            }))
            await asyncio.sleep(CHUNK_MS / 1000.0 / pace)

        await asyncio.sleep(3.0)
        await ws.close()
        try:
            await asyncio.wait_for(rx, timeout=5.0)
        except asyncio.TimeoutError:
            rx.cancel()

    print()
    if errors:
        print(f"!! {len(errors)} error event(s) — arm NOT scorable")
    print(f"--- {len(lines)} segment(s), arm={arm} ---")
    print(" ".join(lines))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("wav")
    p.add_argument("--arm", default="off",
                   help="'off' for no hint, or a BCP-47 tag such as es / pl / ru")
    p.add_argument("--keyterms", default="", help="comma-separated")
    p.add_argument("--pace", type=float, default=1.0, help="1.0 = realtime")
    p.add_argument("--field", default="language_hint",
                   help="xAI reads language_hint; pass 'language' for the "
                        "OpenAI spelling as a negative control")
    p.add_argument("--keyterms-field", dest="kw_field", default="keyterms",
                   help="xAI reads keyterms; 'keywords' is the OpenAI spelling")
    p.add_argument("--start", type=float, default=0.0, help="skip N seconds")
    p.add_argument("--dur", type=float, default=0.0, help="keep N seconds (0 = all)")
    p.add_argument("--snr", type=float, default=99.0,
                   help="add white noise to this SNR in dB; lower = harder")
    p.add_argument("--pad", type=float, default=2.5,
                   help="seconds of trailing silence sent so VAD closes the turn")
    a = p.parse_args()
    terms = [x.strip() for x in a.keyterms.split(",") if x.strip()]
    asyncio.run(run(a.wav, a.arm, terms, a.pace, a.field, a.kw_field,
                    a.start, a.dur, a.snr, a.pad))


if __name__ == "__main__":
    main()
