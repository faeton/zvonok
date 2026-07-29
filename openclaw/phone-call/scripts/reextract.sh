#!/usr/bin/env bash
# Re-read the answers out of a call that already happened.
#
#   reextract.sh <call_id>
#
# Use when result.sh reports processing_status "failed". This costs a text-model
# call and nothing on the phone network: nobody is dialled and nobody is
# disturbed a second time. Placing the call again to fix a failed extraction is
# always the wrong move.
set -euo pipefail

: "${ZVONOK_API_URL:?ZVONOK_API_URL is not set}"
: "${ZVONOK_API_TOKEN:?ZVONOK_API_TOKEN is not set}"

CALL_ID="${1:?usage: reextract.sh <call_id>}"

curl -sS -X POST "$ZVONOK_API_URL/v1/calls/$CALL_ID/reextract" \
  -H "Authorization: Bearer $ZVONOK_API_TOKEN" | python3 -m json.tool
