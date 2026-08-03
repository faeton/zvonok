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

# Zadarma's own published list, read off the "Внешний сервер (SIP URI)" dialog
# in the number settings (BRIEF §5.1). This REPLACED an empirically-derived
# 185.45.152.0/22 that we got by resolving their five SIP hostnames and taking
# the enclosing prefix.
#
# That derivation was not wrong so much as incomplete, and incompletely in the
# direction that matters: outbound worked perfectly, because outbound only ever
# talks to the hosts we resolved. INBOUND is delivered from a different and
# larger set — three of the six ranges below sit outside that /22 entirely — so
# a call to our DID would have been dropped by a default-deny firewall with no
# log, no error at the carrier, and nothing to debug from. The number would
# simply not ring, which is exactly the symptom we already spent time on.
#
# Derived-from-DNS is a reasonable way to start and a bad way to stay. If these
# stop matching the panel, the panel wins.
ZADARMA_NETS=(
  185.45.152.0/24
  185.45.154.0/24
  185.45.155.0/24
  195.122.19.0/27
  31.31.222.192/27
  15.235.128.64/28
)
CONF="/etc/nftables.conf"
MARKER_BEGIN="# --- zvonok (SIP/RTP to Zadarma) --- BEGIN"
MARKER_END="# --- zvonok (SIP/RTP to Zadarma) --- END"
# Legacy opener, kept only so an older persisted block is still recognised and
# replaced rather than duplicated. Do not emit it.
MARKER_LEGACY="# --- zvonok (SIP/RTP to Zadarma) ---"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

echo "==> current input chain:"
nft list chain inet filter input

# One anonymous set rather than a rule per network per port: eighteen rules to
# read through is how a stale one survives a review.
SET="{ $(IFS=,; echo "${ZADARMA_NETS[*]}" | sed 's/,/, /g') }"

RULES=(
  "ip saddr $SET udp dport 5060 accept comment \"zvonok\""
  "ip saddr $SET tcp dport 5060 accept comment \"zvonok\""
  "ip saddr $SET udp dport 10000-20000 accept comment \"zvonok\""
)

TMPDIR_="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_"' EXIT
CANDIDATE="$TMPDIR_/nftables.conf"
LIVE="$TMPDIR_/live.nft"

# --- 1. build the persisted candidate and validate it BEFORE touching anything -
#
# Order matters and used to be wrong: rules were applied live first, so an
# unrelated pre-existing syntax error in $CONF aborted the script AFTER the live
# ruleset had changed — leaving running rules and persisted rules disagreeing,
# with the old ones due back at the next reboot.
#
# The block is delimited at BOTH ends. It used to have only an opening marker
# and the removal stripped from there to EOF, so any rules an administrator
# added after it were silently deleted from the persisted firewall on the next
# run — protections that vanish at reboot and nowhere else.
if grep -qF "$MARKER_BEGIN" "$CONF" || grep -qF "$MARKER_LEGACY" "$CONF"; then
  echo "==> replacing the existing zvonok block in $CONF."
  # Two shapes to recognise. The delimited one is exact. The LEGACY one has no
  # end marker at all, so "delete to EOF" was the old behaviour and it ate
  # whatever an administrator had appended after it. There is no way to know
  # where an unmarked block ends in general — but this one is not general: we
  # wrote it, and it is always a `table inet filter { ... }` closed by a `}` in
  # column 1. Stop there, and never at EOF.
  #
  # Rule order matters: MARKER_BEGIN and MARKER_END both CONTAIN the legacy
  # string, so they must be matched and consumed first or the legacy rule would
  # fire on them.
  awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" -v l="$MARKER_LEGACY" '
    index($0, b)                        { skip = "delim"; next }
    index($0, e)                        { skip = "";      next }
    index($0, l) && skip == ""          { skip = "legacy"; next }
    skip == "legacy" && /^\}[[:space:]]*$/ { skip = "";   next }
    skip == ""
  ' "$CONF" > "$CANDIDATE"
else
  cat "$CONF" > "$CANDIDATE"
fi

{
  echo
  echo "$MARKER_BEGIN"
  echo "# Applied live by deploy/firewall.sh; these lines make it survive reboot."
  echo "# Zadarma's published SIP networks — see BRIEF.md §5.1"
  echo "table inet filter {"
  echo "  chain input {"
  for r in "${RULES[@]}"; do echo "    $r"; done
  echo "  }"
  echo "}"
  echo "$MARKER_END"
} >> "$CANDIDATE"

if ! nft -c -f "$CANDIDATE"; then
  echo "!! candidate config FAILED to parse — nothing applied, $CONF untouched" >&2
  exit 1
fi
echo "==> candidate parses clean (nft -c, nothing applied yet)."

# --- 2. apply live, as ONE transaction ------------------------------------
# Previously this was a loop of `nft delete` followed by three `nft add` calls.
# Each is its own transaction, so a failure partway through `set -e`d out with
# the old rules already gone and only some of the new ones in — i.e. SIP
# half-open or fully closed, depending on where it stopped. `nft -f` applies the
# whole file atomically: either every line lands or none does.
{
  nft -a list chain inet filter input \
    | awk '/comment "zvonok"/ {print "delete rule inet filter input handle " $NF}'
  for r in "${RULES[@]}"; do
    echo "add rule inet filter input $r"
  done
} > "$LIVE"

nft -f "$LIVE"
echo "==> rules applied live (single transaction)."

# --- 3. persist atomically -------------------------------------------------
# `cat > "$CONF"` truncates the real file and rewrites it in place, so a full
# disk or an interruption leaves a half-written, never-validated firewall config
# that only shows up at the next boot. Write beside it and rename: on the same
# filesystem, rename(2) is atomic, so $CONF is only ever the old file or the
# complete new one.
cp -a "$CONF" "${CONF}.bak.$(date +%Y%m%d%H%M%S)"
STAGED="${CONF}.new.$$"
cp -a "$CONF" "$STAGED"          # inherit mode/owner/SELinux context
cat "$CANDIDATE" > "$STAGED"
mv -f "$STAGED" "$CONF"
echo "==> persisted to $CONF (NOT reloaded — intentional)."

# --- 4. final confirmation that what is on disk is loadable ---
if nft -c -f "$CONF"; then
  echo "==> $CONF parses clean."
else
  echo "!! $CONF FAILED to parse — restore from ${CONF}.bak.* before rebooting" >&2
  exit 1
fi


echo "==> resulting input chain:"
nft list chain inet filter input
