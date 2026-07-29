#!/usr/bin/env bash
# Place one phone call. Returns immediately with a call_id — the conversation
# has NOT finished when this returns (see SKILL.md).
#
#   call.sh <+E164> <ru|en|es> "<goal>" ['<answer_schema JSON>']
#
# Example:
#   call.sh +34911234567 es "Find out whether guests can park onsite and the
#     nightly price." '{"type":"object","properties":{
#       "parking_available":{"type":["boolean","null"],"description":"can guests park onsite"},
#       "price_per_night":{"type":["number","null"],"description":"nightly parking price"}}}'
set -euo pipefail

: "${ZVONOK_API_URL:?ZVONOK_API_URL is not set}"
: "${ZVONOK_API_TOKEN:?ZVONOK_API_TOKEN is not set}"

NUMBER="${1:?usage: call.sh <+E164> <ru|en|es> \"<goal>\" ['<answer_schema>']}"
LANGUAGE="${2:?language must be ru, en or es}"
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
print(json.dumps(body))
')

curl -sS -X POST "$ZVONOK_API_URL/v1/calls" \
  -H "Authorization: Bearer $ZVONOK_API_TOKEN" \
  -H 'content-type: application/json' \
  -d "$BODY" | python3 -m json.tool
