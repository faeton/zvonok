#!/usr/bin/env python3
"""Ring a list of numbers with the same one-question playbook, collect answers.

    tools/canvass.py playbooks/pl-pharmacy-ozempic.json numbers.txt

A canvass is the shape zvonok is worst at when driven by hand: fifteen numbers,
one question, and a per-call latency of two minutes means an agent doing it
conversationally spends its whole context waiting. This does the waiting.

Stdlib only, on purpose — it has to run from a laptop with nothing installed.

What it will NOT do:

- exceed the server's concurrency cap. It backs off on 429 rather than hammering,
  because that cap is what stops two calls sharing one SIP channel.
- dial the same number twice in a day. The idempotency key includes the date, so
  re-running after a crash resumes rather than re-dials — that is the whole
  reason to run this instead of a shell loop. `--force` opts out, deliberately
  awkwardly.
- keep going quietly when calls start failing. Five consecutive dial failures
  stop the run: that is a trunk or balance problem, and burning the rest of the
  list against it helps nobody.

Numbers file: one E.164 per line. Everything after `#` is a label for the report.

    +48221234567   # Apteka Centrum, Marszałkowska
    +48221234568   # Apteka Dbam o Zdrowie
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

API_URL = os.environ.get("ZVONOK_API_URL", "http://127.0.0.1:8130").rstrip("/")
API_TOKEN = os.environ.get("ZVONOK_API_TOKEN", "")

# Terminal call states (BRIEF §4). Anything else means still in flight.
TERMINAL = {
    "completed", "busy", "no_answer", "rejected", "voicemail",
    "failed", "canceled", "timed_out", "invalid_number",
}
DONE_PROCESSING = {"completed", "skipped", "failed"}

_print_lock = threading.Lock()


def say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


class Abort:
    """Consecutive-failure fuse.

    A canvass that starts failing usually keeps failing — an expired balance, a
    trunk that stopped authenticating, a country that fell off the allowlist.
    Left alone, a fifteen-number list turns fifteen identical failures into
    fifteen log lines and a confusing report. The fuse makes the run stop and say
    so while the reason is still one line up the screen.
    """

    def __init__(self, limit: int = 5) -> None:
        self.limit = limit
        self.streak = 0
        self.tripped = threading.Event()
        self._lock = threading.Lock()

    def record(self, ok: bool) -> None:
        with self._lock:
            self.streak = 0 if ok else self.streak + 1
            if self.streak >= self.limit:
                self.tripped.set()


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def call_api(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API_URL}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {API_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            detail = str(json.loads(raw).get("detail", raw))
        except json.JSONDecodeError:
            detail = raw
        raise ApiError(e.code, detail[:500]) from None


def read_numbers(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        number, _, label = line.partition("#")
        number = number.strip()
        if number:
            out.append((number, label.strip() or number))
    return out


def place(
    number: str, playbook: dict[str, Any], key: str, abort: "Abort"
) -> dict[str, Any]:
    """Dial, waiting out the concurrency cap rather than failing on it."""
    payload = {
        "number": number,
        "goal": playbook["goal"],
        "language": playbook["language"],
        "idempotency_key": key,
        "wait_seconds": playbook.get("wait_seconds", 15),
    }
    for field in ("answer_schema", "disclosure_level", "introduce_as",
                  "keywords", "max_duration_seconds", "caller_id"):
        if playbook.get(field) is not None:
            payload[field] = playbook[field]

    deadline = time.monotonic() + 900
    while True:
        # Checked on every pass, not just before the first attempt. A worker
        # parked in the sleep below has already been admitted past the fuse, so
        # without this the run prints "STOPPING: 5 calls in a row failed" and
        # then keeps ringing strangers as each parked worker wakes up.
        if abort.tripped.is_set():
            raise ApiError(0, "run aborted after repeated failures")
        try:
            return call_api("POST", "/v1/calls", payload)
        except ApiError as e:
            # 409 = this number is already being called. 429 needs reading: it
            # is BOTH the in-flight concurrency cap (wait, the slot frees in
            # seconds) and the daily call/minute/spend caps (do NOT wait — the
            # allowance resets at midnight, and a run started late in the
            # evening would sleep through the rollover and then quietly start
            # spending tomorrow's budget on numbers nobody re-authorised).
            transient = e.status == 409 or (
                e.status == 429 and "daily" not in e.detail.lower()
            )
            if not transient or time.monotonic() > deadline:
                raise
            time.sleep(20)


def await_result(call_id: str, timeout: float = 600) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    result: dict[str, Any] = {}
    while time.monotonic() < deadline:
        time.sleep(15)
        result = call_api("GET", f"/v1/calls/{call_id}/result")
        if (
            result.get("call_status") in TERMINAL
            and result.get("processing_status") in DONE_PROCESSING
        ):
            return result
    result["timed_out_waiting"] = True
    return result


def one_number(
    number: str, label: str, playbook: dict[str, Any], key: str, abort: Abort
) -> dict[str, Any]:
    """Never raises. One number's failure must not cost the other fourteen.

    ThreadPoolExecutor.map re-raises the FIRST exception when the results are
    consumed — so a single dropped connection while polling used to abort the
    whole run AND throw away the report for every number that had already
    succeeded. The calls had happened and been paid for; only the record of
    them was lost.
    """
    try:
        return _one_number(number, label, playbook, key, abort)
    except Exception as e:  # noqa: BLE001 — deliberately total
        abort.record(ok=False)
        say(f"  ✗ {label}: {type(e).__name__}: {e}")
        return {"number": number, "label": label, "call_status": "error", "error": str(e)}


def _one_number(
    number: str, label: str, playbook: dict[str, Any], key: str, abort: Abort
) -> dict[str, Any]:
    row: dict[str, Any] = {"number": number, "label": label}
    if abort.tripped.is_set():
        return {**row, "call_status": "not_placed", "error": "run aborted after repeated failures"}
    try:
        created = place(number, playbook, key, abort)
    except ApiError as e:
        abort.record(ok=False)
        say(f"  ✗ {label}: refused before dialling — {e.detail}")
        if abort.tripped.is_set():
            say(f"\n  STOPPING: {abort.limit} calls in a row failed to dial. "
                f"Fix that before spending more.")
        return {**row, "error": e.detail, "call_status": "not_placed"}

    row["call_id"] = created["call_id"]
    if created.get("deduplicated"):
        say(f"  = {label}: already called today ({created['call_id']}), reusing")
    else:
        say(f"  → {label}: dialling ({created['call_id']})")

    result = await_result(created["call_id"])
    # A call that rang out or hit an answering machine is not a failure of the
    # SETUP — only states that mean "we could not place calls at all" arm the
    # fuse, or a street of closed pharmacies would stop the run. `rejected` IS
    # one of those: a carrier refusing our INVITE is about us, not about them.
    abort.record(ok=result.get("call_status") not in ("failed", "rejected"))
    row.update({
        "call_status": result.get("call_status"),
        "disposition": result.get("disposition"),
        "duration_seconds": result.get("duration_seconds"),
        "est_cost_usd": result.get("est_cost_usd"),
        "answers": result.get("answers"),
        "summary": result.get("summary"),
        "unreliable_fields": result.get("unreliable_fields"),
    })
    say(f"  ✓ {label}: {row['call_status']} — {result.get('summary') or '(no summary)'}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("playbook", type=Path)
    ap.add_argument("numbers", type=Path)
    ap.add_argument("--concurrency", type=int, default=2,
                    help="parallel calls; the server caps this too (default 2)")
    ap.add_argument("--limit", type=int, help="only the first N numbers")
    ap.add_argument("--out", type=Path, help="write the full result JSON here")
    ap.add_argument("--force", action="store_true",
                    help="dial numbers already called today with this playbook")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not API_TOKEN:
        print("ZVONOK_API_TOKEN is not set", file=sys.stderr)
        return 2

    playbook = json.loads(args.playbook.read_text())
    numbers = read_numbers(args.numbers)
    if args.limit:
        numbers = numbers[: args.limit]
    if not numbers:
        print("no numbers to call", file=sys.stderr)
        return 2

    stamp = date.today().isoformat()
    salt = f":{int(time.time())}" if args.force else ""
    name = playbook.get("name", args.playbook.stem)
    # The idempotency key must change when the REQUEST changes, not just when
    # the day does. Editing a playbook's goal or schema and re-running under the
    # same name on the same date would otherwise hand back yesterday's — well,
    # this morning's — answers to a question we no longer asked.
    shape = json.dumps(
        {k: v for k, v in sorted(playbook.items()) if k != "description"},
        sort_keys=True, ensure_ascii=False,
    )
    revision = hashlib.sha256(shape.encode()).hexdigest()[:8]

    print(f"{name}: {len(numbers)} number(s), {playbook['language']}, "
          f"disclosure={playbook.get('disclosure_level', 'auto')}, "
          f"cap={playbook.get('max_duration_seconds', 300)}s each")
    if args.dry_run:
        for number, label in numbers:
            print(f"  would call {number}  ({label})")
        return 0

    # The first schema property is the question the call is really about — the
    # agent is told to ask it first, so it is what the report leads with.
    props = ((playbook.get("answer_schema") or {}).get("properties") or {})
    headline = next(iter(props), None)

    def lead(row: dict[str, Any]) -> Any:
        answers = row.get("answers") or {}
        return answers.get(headline) if headline else next(iter(answers.values()), None)

    abort = Abort()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        rows = list(pool.map(
            lambda item: one_number(
                item[0], item[1], playbook,
                f"{name}:{revision}:{item[0]}:{stamp}{salt}", abort
            ),
            numbers,
        ))

    column = (headline or "answer")[:12]
    print(f"\n  {'number':<16}{'status':<14}{column:<14}summary")
    for row in rows:
        value = lead(row)
        flag = {True: "YES", False: "no", None: "—"}.get(value, str(value))
        print(f"  {row['number']:<16}{str(row.get('call_status')):<14}{flag:<14}"
              f"{row.get('summary') or row.get('error') or ''}")

    spent = sum(r.get("est_cost_usd") or 0 for r in rows)
    hits = [r for r in rows if lead(r) is True]
    print(f"\n{len(rows)} called in {int(time.monotonic() - started)}s, "
          f"~${spent:.2f}, {len(hits)} yes")
    if abort.tripped.is_set():
        print("RUN ABORTED after repeated dial failures — some numbers were never called.")

    out = args.out or Path(f"canvass-{name}-{stamp}.json")
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"full results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
