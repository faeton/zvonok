#!/usr/bin/env bash
# Install the phone-call skill into OpenClaw on de1. Run FROM de1.
#
#   sudo ./install.sh
#
# OpenClaw runs as its own user (`debian`), so this needs root to write into
# its skills directory. Two things happen:
#
#   1. openclaw/phone-call/ is copied to /home/debian/.openclaw/skills/phone-call
#      Skills are auto-discovered from that directory.
#   2. An entry is added to `skills.entries` in openclaw.json supplying
#      ZVONOK_API_URL and the `openclaw` bearer token. That token is read from
#      zvonok's own deploy/.env — it is never typed in here and never committed.
#
# The config is backed up first, and the edit refuses to run if the result would
# not parse as JSON. openclaw.json drives a live service; a truncated write there
# is an outage.
set -euo pipefail

OPENCLAW_HOME=/home/debian/.openclaw
CONFIG="$OPENCLAW_HOME/openclaw.json"
SKILLS="$OPENCLAW_HOME/skills"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HERE/../deploy/.env"

[[ $EUID -eq 0 ]] || { echo "run with sudo — OpenClaw lives in debian's home" >&2; exit 1; }
[[ -f "$CONFIG" ]] || { echo "no $CONFIG — is OpenClaw installed here?" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "no $ENV_FILE — run this from the zvonok checkout on de1" >&2; exit 1; }

# --- the token -------------------------------------------------------------
# ZVONOK_API_TOKENS is "mac-claude:xxx,openclaw:yyy". OpenClaw gets its OWN
# identity so its caps and its audit trail are separate from the Mac's.
TOKENS=$(grep '^ZVONOK_API_TOKENS=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
TOKEN=$(printf '%s' "$TOKENS" | tr ',' '\n' | grep '^openclaw:' | cut -d: -f2-)
[[ -n "$TOKEN" ]] || { echo "no openclaw: token in ZVONOK_API_TOKENS" >&2; exit 1; }

BIND=$(grep '^ZVONOK_BIND_HOST=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
PORT=$(grep '^ZVONOK_API_PORT=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
API_URL="http://${BIND}:${PORT}"

echo "==> call-api at $API_URL"
curl -fsS "$API_URL/healthz" >/dev/null || {
  echo "call-api is not answering at $API_URL — start it before installing" >&2
  exit 1
}

# --- the skill files -------------------------------------------------------
echo "==> installing skill files"
install -d -o debian -g debian "$SKILLS/phone-call"
rm -rf "$SKILLS/phone-call"
cp -r "$HERE/phone-call" "$SKILLS/phone-call"
chown -R debian:debian "$SKILLS/phone-call"
chmod +x "$SKILLS/phone-call/scripts/"*.sh

# --- the config entry ------------------------------------------------------
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
cp -a "$CONFIG" "$CONFIG.bak-phonecall-$STAMP"
echo "==> backed up config to $CONFIG.bak-phonecall-$STAMP"

# Write to a temp file and validate before swapping: openclaw.json is read by a
# running gateway, and a half-written file is a service outage rather than a
# failed script.
TMP=$(mktemp)
API_URL="$API_URL" TOKEN="$TOKEN" CONFIG="$CONFIG" python3 - "$TMP" <<'PY'
import json, os, sys

out_path = sys.argv[1]
with open(os.environ["CONFIG"]) as fh:
    config = json.load(fh)

entries = config.setdefault("skills", {}).setdefault("entries", {})
entries["phone-call"] = {
    "enabled": True,
    "env": {
        "ZVONOK_API_URL": os.environ["API_URL"],
        "ZVONOK_API_TOKEN": os.environ["TOKEN"],
    },
}

with open(out_path, "w") as fh:
    json.dump(config, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PY

python3 -m json.tool "$TMP" >/dev/null || { echo "refusing to install: result is not valid JSON" >&2; rm -f "$TMP"; exit 1; }
cat "$TMP" > "$CONFIG"      # preserve owner/mode of the original
rm -f "$TMP"
chown debian:debian "$CONFIG"

echo "==> registered skills.entries['phone-call']"
echo
echo "Restart the gateway so it picks up the new skill:"
echo "  sudo -u debian XDG_RUNTIME_DIR=/run/user/\$(id -u debian) systemctl --user restart openclaw-gateway"
echo
echo "Then ask OpenClaw something like:"
echo "  \"call +34XXXXXXXXX and ask what time they close today\""
