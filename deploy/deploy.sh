#!/usr/bin/env bash
# Bring up / refresh the zvonok stack on de1. Run FROM de1, in this directory.
set -euo pipefail

cd "$(dirname "$0")"

# The real secrets live OUTSIDE the deploy tree, and `deploy/.env` is a symlink
# to them. This is not tidiness — it is damage control for a specific accident
# that has already happened once.
#
# `deploy/.env` is gitignored, so it exists only on de1. A `rsync -a --delete`
# from the laptop therefore sees a file the source does not have and removes it:
# every key, token and DID gone in one command, with no warning, from a command
# whose purpose was to copy code. The stack keeps running (the containers hold
# their env), so nothing looks broken until the next deploy.
#
# With the file kept at CANONICAL below, an rsync --delete can destroy at most
# the symlink, and this script rebuilds it on the next run.
CANONICAL="${ZVONOK_ENV_FILE:-$HOME/.config/zvonok/env}"

if [[ ! -e .env && -f "$CANONICAL" ]]; then
  ln -s "$CANONICAL" .env
  echo "==> deploy/.env was missing — relinked to $CANONICAL"
fi

if [[ ! -e .env ]]; then
  echo "no .env, and nothing at $CANONICAL either." >&2
  echo "Fill in a copy of .env.example there, then:" >&2
  echo "  mkdir -p \"\$(dirname \"$CANONICAL\")\" && chmod 700 \"\$(dirname \"$CANONICAL\")\"" >&2
  echo "  ln -s \"$CANONICAL\" deploy/.env" >&2
  exit 1
fi
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
# Per tenant, because each worker mounts only its own: the janitor's disk
# recovery settles a call and bills extraction without any token, so a shared
# directory let any worker drop a file named for another tenant's job id.
mkdir -p transcripts
for tenant_dir in transcripts transcripts/default ${ZVONOK_TRANSCRIPT_TENANTS:-}; do
  mkdir -p "$tenant_dir"
  if [[ "$(stat -c %u "$tenant_dir")" != "10001" ]]; then
    sudo chown 10001:10001 "$tenant_dir"
    echo "==> chowned $tenant_dir/ to uid 10001 (agent container user)"
  fi
done

docker compose up -d --build

echo
echo "==> containers:"
docker compose ps
