#!/usr/bin/env bash
# Place one test call. Phase-1 manual trigger; phase 2 replaces this with
# POST /v1/calls (BRIEF §5.4).
#
#   ./call.sh +34600123456 "Ask whether they have parking and the nightly price." en
#
# Caller ID defaults to ZVONOK_DEFAULT_CALLER_ID from .env: EU/UK destinations
# are ×20-34 cheaper with a UK caller ID than a UA one (BRIEF §9 phase-0).
set -euo pipefail

cd "$(dirname "$0")"
[ -f .env ] && . ./.env

NUMBER="${1:?usage: call.sh <+E164> [goal] [lang] [caller_id]}"
GOAL="${2:-Confirm you reached the right person, then thank them and end the call.}"
LANG_="${3:-en}"
CALLER_ID="${4:-${ZVONOK_DEFAULT_CALLER_ID:?no caller_id given and ZVONOK_DEFAULT_CALLER_ID not in .env}}"

./lkctl.sh dispatch \
  --number "$NUMBER" \
  --goal "$GOAL" \
  --language "$LANG_" \
  --caller-id "$CALLER_ID"

echo
echo "==> follow along:  docker compose logs -f agent"
echo "==> transcript:    ls -t transcripts/ | head -1"
