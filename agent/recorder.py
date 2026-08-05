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
import wave
from pathlib import Path
from typing import Any

logger = logging.getLogger("zvonok.recorder")

# Same directory the transcript goes to, so a call is one prefix and everything
# about it sorts together.
TRANSCRIPT_DIR = Path(os.getenv("ZVONOK_TRANSCRIPT_DIR", "/data/transcripts"))


class SideRecorder:
    """One direction of one call.

    Opens lazily on the first frame, because the frame is what tells us the
    sample rate and channel count — the realtime model and the SIP leg do not
    agree on either, and guessing produces a file that plays at the wrong speed,
    which is the most confusing possible failure for something whose whole job
    is to be listened to.
    """

    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self.label = label
        self._wav: wave.Wave_write | None = None
        self._frames = 0
        self._broken = False

    def write(self, frame: Any) -> None:  # noqa: ANN401 — rtc.AudioFrame
        """Never raises. A recorder that can kill a call is worse than no recorder."""
        if self._broken:
            return
        try:
            if self._wav is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._wav = wave.open(str(self.path), "wb")
                self._wav.setnchannels(frame.num_channels)
                self._wav.setsampwidth(2)  # int16, which is what rtc gives us
                self._wav.setframerate(frame.sample_rate)
                logger.info("recording %s → %s (%d Hz, %d ch)",
                            self.label, self.path.name,
                            frame.sample_rate, frame.num_channels)
            self._wav.writeframes(bytes(frame.data))
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

    def __init__(self, call_id: str, enabled: bool, stamp: str) -> None:
        self.enabled = enabled
        if not enabled:
            self.ours = self.theirs = None
            return
        base = TRANSCRIPT_DIR / f"{stamp}-{call_id}"
        self.ours = SideRecorder(Path(f"{base}-agent.wav"), "agent")
        self.theirs = SideRecorder(Path(f"{base}-callee.wav"), "callee")

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
