#!/usr/bin/env bash
# What have we already called this number about?
#
#   contacts.sh <+E164>
#
# Answers the question SKILL.md's "never call the same place twice" rule
# depends on. Costs nothing, dials nobody, and is the only way to find out that
# the thing you are about to ring for was already asked and answered on Tuesday.
#
# ⚠ Read the privacy rule in SKILL.md before using this on an INBOUND caller.
# Caller ID identifies a line, not a person, and it is trivially spoofed.
set -euo pipefail

: "${ZVONOK_API_URL:?ZVONOK_API_URL is not set}"
: "${ZVONOK_API_TOKEN:?ZVONOK_API_TOKEN is not set}"

NUMBER="${1:?usage: contacts.sh <+E164>}"

# `+` is a live character in a URL path (it means space in a query string, and
# some proxies normalise it in paths too), so encode it rather than hoping.
ENCODED=$(NUMBER="$NUMBER" python3 -c '
import os, urllib.parse
print(urllib.parse.quote(os.environ["NUMBER"], safe=""))
')

curl -sS "$ZVONOK_API_URL/v1/contacts/$ENCODED" \
  -H "Authorization: Bearer $ZVONOK_API_TOKEN" | python3 -m json.tool
