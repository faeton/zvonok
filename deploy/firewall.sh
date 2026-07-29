#!/usr/bin/env bash
# Open SIP + RTP to Zadarma only. Run on de1 as root.
#
# de1 runs nftables with `policy drop` on input (BRIEF §5.2), so these ports are
# closed until this script runs — and everything NOT listed here stays closed.
#
# ⚠ /etc/nftables.conf begins with `flush ruleset`. Restarting the nftables
# service mid-session wipes Docker's iptables-nft chains until Docker is also
# restarted. So this script adds rules LIVE and persists them to the config file
# separately. It never restarts the service. Do not "simplify" that away.

set -euo pipefail

ZADARMA_NET="185.45.152.0/22"   # AS199790, derivation in BRIEF §5.1
CONF="/etc/nftables.conf"
MARKER="# --- zvonok (SIP/RTP to Zadarma) ---"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

echo "==> current input chain:"
nft list chain inet filter input

# --- 1. apply live (idempotent: wipe our rules first, by comment handle) ---
for handle in $(nft -a list chain inet filter input \
                 | awk '/comment "zvonok"/ {print $NF}'); do
  nft delete rule inet filter input handle "$handle"
done

nft add rule inet filter input ip saddr "$ZADARMA_NET" udp dport 5060 accept comment \"zvonok\"
nft add rule inet filter input ip saddr "$ZADARMA_NET" tcp dport 5060 accept comment \"zvonok\"
nft add rule inet filter input ip saddr "$ZADARMA_NET" udp dport 10000-20000 accept comment \"zvonok\"

echo "==> rules applied live."

# --- 2. persist: build a candidate, validate it, and only then swap it in ---
# Never edit $CONF in place before validating. If the appended block turns out to
# be invalid (nftables syntax drift, an already-malformed file), an in-place edit
# leaves the host with a persistent config that fails to load at next boot —
# i.e. no firewall — while the live rules still look fine.
if grep -qF "$MARKER" "$CONF"; then
  echo "==> $CONF already carries the zvonok block; leaving it alone."
else
  CANDIDATE="$(mktemp)"
  trap 'rm -f "$CANDIDATE"' EXIT
  cat "$CONF" > "$CANDIDATE"
  cat >> "$CANDIDATE" <<EOF

$MARKER
# Applied live by deploy/firewall.sh; these lines make it survive reboot.
# Zadarma network AS199790 — see BRIEF.md §5.1
table inet filter {
  chain input {
    ip saddr $ZADARMA_NET udp dport 5060 accept comment "zvonok"
    ip saddr $ZADARMA_NET tcp dport 5060 accept comment "zvonok"
    ip saddr $ZADARMA_NET udp dport 10000-20000 accept comment "zvonok"
  }
}
EOF

  if ! nft -c -f "$CANDIDATE"; then
    echo "!! candidate config FAILED to parse — $CONF left untouched" >&2
    exit 1
  fi
  echo "==> candidate parses clean (nft -c, nothing applied)."

  cp -a "$CONF" "${CONF}.bak.$(date +%Y%m%d%H%M%S)"
  cat "$CANDIDATE" > "$CONF"     # preserves original mode/owner of $CONF
  echo "==> persisted to $CONF (NOT reloaded — intentional)."
fi

# --- 3. final confirmation that what is on disk is loadable ---
if nft -c -f "$CONF"; then
  echo "==> $CONF parses clean."
else
  echo "!! $CONF FAILED to parse — fix before rebooting or you lose the ruleset" >&2
  exit 1
fi

echo
echo "==> resulting input chain:"
nft list chain inet filter input
