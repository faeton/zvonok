#!/usr/bin/env bash
# Run lkctl.py inside the agent image (which already has livekit-api) so de1
# needs no extra tooling. Host networking → reaches livekit-server on loopback.
#
#   ./lkctl.sh trunks
#   ./lkctl.sh create-trunk
#   ./lkctl.sh dispatch --number +34600123456 --goal "..." --language en
set -euo pipefail

cd "$(dirname "$0")"
set -a; . ./.env; set +a

exec docker run --rm --network host \
  -e LIVEKIT_API_KEY -e LIVEKIT_API_SECRET \
  -e ZVONOK_DEFAULT_CALLER_ID -e ZVONOK_OWNED_CALLER_IDS \
  -v "$PWD/lkctl.py:/app/lkctl.py:ro" \
  zvonok-agent:phase1 python -u /app/lkctl.py "$@"
