#!/usr/bin/env bash
# Bring up / refresh the zvonok stack on de1. Run FROM de1, in this directory.
set -euo pipefail

cd "$(dirname "$0")"

[[ -f .env ]] || { echo "no .env — copy .env.example and fill it in" >&2; exit 1; }
set -a; . ./.env; set +a

: "${LIVEKIT_API_KEY:?set in .env}"
: "${LIVEKIT_API_SECRET:?set in .env}"
: "${XAI_API_KEY:?set in .env}"
: "${POSTGRES_PASSWORD:?set in .env}"
: "${ZVONOK_API_TOKENS:?set in .env}"
: "${ZVONOK_INTERNAL_TOKEN:?set in .env}"
: "${ZVONOK_BIND_HOST:?set in .env — de1 tailnet IP, see \`tailscale ip -4\`}"

# call-api binds to the tailnet address rather than 0.0.0.0, so a tailscale that
# is down at container start means a bind failure and a restart loop. Better to
# say so here than to debug it from a crash log.
if ! ip -4 addr show | grep -q "inet ${ZVONOK_BIND_HOST}/"; then
  echo "ZVONOK_BIND_HOST=${ZVONOK_BIND_HOST} is not an address on this host" >&2
  echo "(is tailscale up?  tailscale ip -4)" >&2
  exit 1
fi

# livekit.yaml is committed with placeholders so no secret ever lands in git.
# Render the real one next to it at deploy time.
sed -e "s|PLACEHOLDER_KEY|${LIVEKIT_API_KEY}|" \
    -e "s|PLACEHOLDER_SECRET|${LIVEKIT_API_SECRET}|" \
    livekit.yaml > livekit.rendered.yaml
chmod 600 livekit.rendered.yaml

# The agent container runs as uid 10001 (non-root); the bind-mounted transcript
# dir is created by the host user, so it must be chowned or writes fail with
# EPERM after the call has already happened — the worst time to find out.
mkdir -p transcripts
if [[ "$(stat -c %u transcripts)" != "10001" ]]; then
  sudo chown 10001:10001 transcripts
  echo "==> chowned transcripts/ to uid 10001 (agent container user)"
fi

docker compose up -d --build

echo
echo "==> containers:"
docker compose ps
