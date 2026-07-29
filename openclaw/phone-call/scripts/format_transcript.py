#!/usr/bin/env python3
"""Render a transcript JSON payload (on stdin) for a human to read.

A separate file rather than a `python3 -c` one-liner inside result.sh: the
formatting needs both quote characters and an f-string, which does not survive
being nested inside a shell string.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    doc = json.load(sys.stdin)

    # An error payload has `detail` and no `turns`. Without this branch the
    # "no turns recorded" message below would be printed for a call we simply
    # cannot see — and "the line was silent" is a very different conclusion from
    # "that call id belongs to another agent".
    if "turns" not in doc:
        print("could not read the transcript: " + str(doc.get("detail", doc)))
        sys.exit(1)

    if doc.get("redacted"):
        print("[redacted] The callee declined to be transcribed, so their words")
        print("were discarded as promised. Only an audit record remains.")
        return

    turns = doc.get("turns") or []
    if not turns:
        print("(no turns recorded — the line may have answered without speech)")
        return

    for turn in turns:
        who = "callee" if turn.get("speaker") in ("user", "callee") else "Klava"
        # An interrupted turn holds words the other person may never have heard,
        # so never quote one back to the user as something that was said to them.
        mark = " [INTERRUPTED - may not have been heard]" if turn.get("interrupted") else ""
        confidence = turn.get("confidence")
        if who == "callee" and confidence is not None and float(confidence) < 0.6:
            mark += " [heard poorly]"
        t = turn.get("t")
        stamp = "%6.1fs " % float(t) if t is not None else ""
        print(stamp + who + mark + ": " + (turn.get("text") or ""))


if __name__ == "__main__":
    main()
