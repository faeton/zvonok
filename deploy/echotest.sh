#!/usr/bin/env bash
# Zadarma's echo test (4444): it answers, greets, records, then plays your audio
# back. Free, always up, and never annoyed — so it is the cheapest proof that
# the boring layer works before any real number is dialled (BRIEF §5.1).
#
#   ./echotest.sh              # full agent through the trunk
#   ./echotest.sh 60           # ...for up to 60s instead of 45
#
# What it proves: trunk accepted our INVITE on IP auth, alaw negotiated, RTP
# flows in *both* directions (one-way audio shows up as an empty transcript
# with clean SIP logs), and the agent's own speech reaches the far end.
#
# What it does NOT prove: anything conversational. The agent hears its own
# voice played back and will happily answer itself, so turn-taking, answerer
# detection and ASR accuracy all read as garbage here — by design, not by bug.
# Judge the media path from this; judge behaviour from a real call.
#
# 4444 is deliberately unreachable through POST /v1/calls: it is not E.164 and
# policy.normalise_number rejects it. That is correct — a short-code hole in
# the destination allowlist is exactly the kind of thing that later dials
# something billable. This script takes the same bypass as call.sh.
set -euo pipefail

cd "$(dirname "$0")"
[ -f .env ] && . ./.env

MAX_DURATION="${1:-45}"
CALLER_ID="${2:-${ZVONOK_DEFAULT_CALLER_ID:?ZVONOK_DEFAULT_CALLER_ID not in .env}}"

./lkctl.sh dispatch \
  --number 4444 \
  --goal "You have reached an automated echo test, not a person. Say clearly and slowly: 'Echo test, one, two, three, four, five.' Then stop talking. You will hear your own voice repeated back — that is the test working, not someone replying, so do not respond to it. End the call after the playback." \
  --language en \
  --caller-id "$CALLER_ID" \
  --max-duration "$MAX_DURATION"

echo
echo "==> follow along:  docker compose logs -f agent"
echo "==> transcript:    ls -t transcripts/ | head -1"
echo
echo "Reading it: the agent's own line present and the echo transcribed back"
echo "means both media directions are alive. Agent line but no echo = inbound"
echo "RTP is dead (check use_external_ip). Neither = check the SIP logs first."
