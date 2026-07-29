#!/usr/bin/env bash
# Fetch the outcome of a call. Run this 60-180 seconds after call.sh.
#
#   result.sh <call_id> [--transcript]
#
# If processing_status is "pending" or "extracting", the call or its analysis is
# still running — wait ~30s and ask again. If it is "failed", the CALL succeeded
# and only the answer-reading failed: use reextract.sh, never a second call.
set -euo pipefail

: "${ZVONOK_API_URL:?ZVONOK_API_URL is not set}"
: "${ZVONOK_API_TOKEN:?ZVONOK_API_TOKEN is not set}"

CALL_ID="${1:?usage: result.sh <call_id> [--transcript]}"
WANT_TRANSCRIPT="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"

auth=(-H "Authorization: Bearer $ZVONOK_API_TOKEN")

# Capture the status separately: a 404 here means this agent cannot see that call
# id (each identity only sees its own), and reporting that plainly beats printing
# an error body and then carrying on to fetch a transcript that cannot exist.
BODY=$(curl -sS -w '\n%{http_code}' "${auth[@]}" "$ZVONOK_API_URL/v1/calls/$CALL_ID/result")
STATUS="${BODY##*$'\n'}"
printf '%s\n' "${BODY%$'\n'*}" | python3 -m json.tool

if [[ "$STATUS" != "200" ]]; then
  echo "(HTTP $STATUS — no result to report)" >&2
  exit 1
fi

if [[ "$WANT_TRANSCRIPT" == "--transcript" ]]; then
  echo
  echo "=== transcript ==="
  curl -sS "${auth[@]}" "$ZVONOK_API_URL/v1/calls/$CALL_ID/transcript" \
    | python3 "$HERE/format_transcript.py"
fi
