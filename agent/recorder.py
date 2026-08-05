"""Write the two sides of a call to disk, as audio, when asked to.

⚠ OFF BY DEFAULT AND IT MUST STAY THAT WAY. Everything else in this service
keeps TEXT, and §8's disclosure says so in as many words — "I'll note the answer
down", not "this call is recorded". That wording was chosen because it is TRUE,
and it stops being true the moment this module writes a file. So recording is a
per-call flag (BRIEF §312), the flag forces a disclosure that says recording out
loud, and neither half is optional.

The honest use for it is the one it was built for: hearing how WE sound. Three
Figueres pharmacies took our question in full and hung up within three seconds,
and no transcript can say whether the voice was robotic, clipped, too loud, or
arrived after a silence long enough to look like a dead line. For that, a call
to a number we own answers everything and involves no stranger at all.

Two files, not a mix, because the diagnosis is usually about TIMING: with the
sides apart you can see the gap between their last word and our first, which is
the number a mixed track hides.

Deliberately stdlib-only (`wave`). This runs inside the call, in the audio path,
on a box that is also paying for the PSTN leg — a codec dependency here would be
a new way for a call to fail while somebody is on the line.
"""

from __future__ import annotations

import logging
import os
import time
import wave
from pathlib import Path
from typing import Any

logger = logging.getLogger("zvonok.recorder")

# Same directory the transcript goes to, so a call is one prefix and everything
# about it sorts together.
TRANSCRIPT_DIR = Path(os.getenv("ZVONOK_TRANSCRIPT_DIR", "/data/transcripts"))


class SideRecorder:
    """One direction of one call, on the call's clock.

    Opens lazily on the first frame, because the frame is what tells us the
    sample rate and channel count — the realtime model and the SIP leg do not
    agree on either, and guessing produces a file that plays at the wrong speed,
    which is the most confusing possible failure for something whose whole job
    is to be listened to.

    ⚠ SILENCE IS WRITTEN, NOT SKIPPED, and that is the whole point of the file.
    The outgoing side only produces frames while the agent is SPEAKING, so a
    naive recorder concatenates its utterances and throws the gaps away: the
    first attempt gave a 4.7-second agent track for a 30-second call. It sounded
    fine and was useless, because the question these recordings exist to answer
    is "how long did the other person sit in silence before we answered" — and
    that is precisely what had been deleted.

    So each side is padded from a t0 SHARED with the other side. Both files then
    start at the same instant of the same call and can be lined up in any
    editor, even though their sample rates differ.
    """

    def __init__(self, path: Path, label: str, t_zero: float) -> None:
        self.path = path
        self.label = label
        self._t_zero = t_zero
        self._wav: wave.Wave_write | None = None
        self._frames = 0
        self._written_samples = 0
        self._rate = 0
        self._channels = 1
        self._broken = False

    def _pad_to_now(self) -> None:
        """Fill the gap since the last frame with real silence."""
        elapsed = max(0.0, time.monotonic() - self._t_zero)
        expected = int(elapsed * self._rate)
        missing = expected - self._written_samples
        if missing <= 0:
            return
        # A cap, because a stalled track must not turn into a gigabyte of zeros:
        # 30 s of silence is already far past anything diagnostic.
        missing = min(missing, self._rate * 30)
        self._wav.writeframes(b"\x00\x00" * (missing * self._channels))
        self._written_samples += missing

    def write(self, frame: Any) -> None:  # noqa: ANN401 — rtc.AudioFrame
        """Never raises. A recorder that can kill a call is worse than no recorder."""
        if self._broken:
            return
        try:
            if self._wav is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._wav = wave.open(str(self.path), "wb")
                self._rate = frame.sample_rate
                self._channels = frame.num_channels
                self._wav.setnchannels(self._channels)
                self._wav.setsampwidth(2)  # int16, which is what rtc gives us
                self._wav.setframerate(self._rate)
                logger.info("recording %s → %s (%d Hz, %d ch)",
                            self.label, self.path.name, self._rate, self._channels)
            self._pad_to_now()
            self._wav.writeframes(bytes(frame.data))
            self._written_samples += frame.samples_per_channel
            self._frames += 1
        except Exception as e:  # noqa: BLE001
            # Once, loudly, and then never again for this call: a per-frame log
            # on a broken disk would write thousands of lines in a few seconds.
            self._broken = True
            logger.warning("recording %s failed, giving up on it: %r", self.label, e)

    def close(self) -> None:
        if self._wav is None:
            return
        try:
            self._wav.close()
            logger.info("recorded %s: %d frames → %s", self.label, self._frames,
                        self.path.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("could not close %s recording: %r", self.label, e)
        finally:
            self._wav = None


class CallRecorder:
    """Both sides of one call, or nothing at all when the flag is off."""

    def __init__(self, call_id: str, enabled: bool, stamp: str,
                 t_zero: float | None = None) -> None:
        self.enabled = enabled
        if not enabled:
            self.ours = self.theirs = None
            return
        # ONE t0 for both sides. Two independently-started clocks would each be
        # internally consistent and mutually useless, which is the failure this
        # whole padding scheme exists to avoid.
        t0 = time.monotonic() if t_zero is None else t_zero
        base = TRANSCRIPT_DIR / f"{stamp}-{call_id}"
        self.ours = SideRecorder(Path(f"{base}-agent.wav"), "agent", t0)
        self.theirs = SideRecorder(Path(f"{base}-callee.wav"), "callee", t0)

    def write_ours(self, frame: Any) -> None:  # noqa: ANN401
        if self.ours is not None:
            self.ours.write(frame)

    def write_theirs(self, frame: Any) -> None:  # noqa: ANN401
        if self.theirs is not None:
            self.theirs.write(frame)

    def close(self) -> None:
        for side in (self.ours, self.theirs):
            if side is not None:
                side.close()
