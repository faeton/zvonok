#!/usr/bin/env bash
# Place one phone call. Returns immediately with a call_id — the conversation
# has NOT finished when this returns (see SKILL.md).
#
#   call.sh <+E164> <ru|en|es|pl> "<goal>" ['<answer_schema JSON>']
#
# Optional env: INTRODUCE_AS, KEYWORDS (see SKILL.md).
#
# Example:
#   call.sh +34911234567 es "Find out whether guests can park onsite and the
#     nightly price." '{"type":"object","properties":{
#       "parking_available":{"type":["boolean","null"],"description":"can guests park onsite"},
#       "price_per_night":{"type":["number","null"],"description":"nightly parking price"}}}'
set -euo pipefail

: "${ZVONOK_API_URL:?ZVONOK_API_URL is not set}"
: "${ZVONOK_API_TOKEN:?ZVONOK_API_TOKEN is not set}"

NUMBER="${1:?usage: call.sh <+E164> <ru|en|es|pl> \"<goal>\" ['<answer_schema>']}"
LANGUAGE="${2:?language must be ru, en, es or pl}"
GOAL="${3:?a goal is required}"
SCHEMA="${4:-}"

# Build the body in python rather than by string-pasting: a goal legitimately
# contains apostrophes, quotes and newlines, and hand-built JSON breaks on them.
BODY=$(NUMBER="$NUMBER" LANGUAGE="$LANGUAGE" GOAL="$GOAL" SCHEMA="$SCHEMA" python3 -c '
import json, os, sys

body = {
    "number": os.environ["NUMBER"],
    "language": os.environ["LANGUAGE"],
    "goal": os.environ["GOAL"],
    # Long enough to catch a busy signal or a bad number, far too short to wait
    # out a conversation — which is the point (see SKILL.md).
    "wait_seconds": 12,
}
schema = os.environ.get("SCHEMA") or ""
if schema.strip():
    try:
        body["answer_schema"] = json.loads(schema)
    except json.JSONDecodeError as e:
        sys.exit(f"answer_schema is not valid JSON: {e}")
# Optional: who the assistant says it is calling for (see SKILL.md).
# In Russian, supply the genitive: INTRODUCE_AS="вашего постоянного клиента".
introduce = os.environ.get("INTRODUCE_AS") or ""
if introduce.strip():
    body["introduce_as"] = introduce.strip()
# Optional: proper nouns to bias the recogniser toward (see SKILL.md). A phone
# line is 8 kHz and destroys exactly the words a call turns on — a drug, a
# brand, a part number — so naming them up front is the cheapest quality win
# available. Comma-separated.
keywords = [k.strip() for k in (os.environ.get("KEYWORDS") or "").split(",") if k.strip()]
if keywords:
    body["keywords"] = keywords
print(json.dumps(body))
')

curl -sS -X POST "$ZVONOK_API_URL/v1/calls" \
  -H "Authorization: Bearer $ZVONOK_API_TOKEN" \
  -H 'content-type: application/json' \
  -d "$BODY" | python3 -m json.tool
