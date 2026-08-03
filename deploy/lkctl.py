#!/usr/bin/env python3
"""Minimal LiveKit control helper for zvonok phase 1.

Does the two things the `lk` CLI would do for us — create the outbound trunk and
dispatch a call job — using the livekit-api SDK that is already inside the agent
image. Keeps de1 free of an extra system-wide binary.

    python lkctl.py trunks
    python lkctl.py create-trunk
    python lkctl.py dispatch --number +34600123456 --goal "..." --language en
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from livekit import api

URL = os.getenv("LIVEKIT_HTTP_URL", "http://127.0.0.1:7880")
KEY = os.environ["LIVEKIT_API_KEY"]
SECRET = os.environ["LIVEKIT_API_SECRET"]

# Caller IDs are deployment data (they identify a real SIP account), so they
# come from .env, never from this file. The default matters: EU/UK destinations
# measured ×20-34 cheaper with a UK caller ID than a UA one (BRIEF §9 phase-0)
# — a cost lever, not cosmetics.
DEFAULT_CALLER_ID = os.getenv("ZVONOK_DEFAULT_CALLER_ID", "")
ALL_DIDS = [
    x.strip() for x in os.getenv("ZVONOK_OWNED_CALLER_IDS", "").split(",") if x.strip()
]


def lkapi() -> api.LiveKitAPI:
    return api.LiveKitAPI(url=URL, api_key=KEY, api_secret=SECRET)


async def cmd_trunks(_: argparse.Namespace) -> None:
    async with lkapi() as lk:
        resp = await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
        if not resp.items:
            print("(no outbound trunks)")
        for t in resp.items:
            print(f"{t.sip_trunk_id}  name={t.name!r}  address={t.address}  numbers={list(t.numbers)}")


async def cmd_create_trunk(args: argparse.Namespace) -> None:
    async with lkapi() as lk:
        existing = await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
        for t in existing.items:
            if t.name == args.name:
                print(f"trunk {args.name!r} already exists: {t.sip_trunk_id}")
                return

        # NO auth_username/auth_password: Zadarma authorizes the host's static
        # source IP. LiveKit's docs discourage IP auth, but that warning targets
        # LiveKit Cloud's non-static egress — irrelevant self-hosted.
        if not ALL_DIDS:
            raise SystemExit("set ZVONOK_OWNED_CALLER_IDS in .env before creating the trunk")
        trunk = api.SIPOutboundTrunkInfo(
            name=args.name,
            address="sip.zadarma.com",
            transport=api.SIPTransport.SIP_TRANSPORT_UDP,
            numbers=ALL_DIDS,
        )
        resp = await lk.sip.create_outbound_trunk(
            api.CreateSIPOutboundTrunkRequest(trunk=trunk)
        )
        print(f"created: {resp.sip_trunk_id}")
        print()
        print(f"put this in deploy/.env:  SIP_OUTBOUND_TRUNK_ID={resp.sip_trunk_id}")


# Signalling sources Zadarma may send us an INVITE from (BRIEF §5.1). This is
# the SAME block the host firewall scopes 5060 to, and it is duplicated here on
# purpose: the firewall decides whether a packet reaches the box, this decides
# whether livekit-sip believes it. Either alone is a way to answer calls from
# strangers on a port that is open to the internet.
# Zadarma's PUBLISHED list, from the "External server (SIP URI)" dialog in the
# number settings — not the 185.45.152.0/22 we previously derived by resolving
# their SIP hostnames. Three of these sit outside that /22, and they are the
# ones INBOUND arrives from, so the derived prefix was fine for placing calls
# and silently wrong for receiving them. Keep in step with deploy/firewall.sh:
# the firewall decides whether a packet reaches the box, this decides whether
# livekit-sip believes it, and a call needs both.
ZADARMA_SIP_NETS = [
    "185.45.152.0/24",
    "185.45.154.0/24",
    "185.45.155.0/24",
    "195.122.19.0/27",
    "31.31.222.192/27",
    "15.235.128.64/28",
]


async def cmd_inbound_trunks(_: argparse.Namespace) -> None:
    async with lkapi() as lk:
        resp = await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
        if not resp.items:
            print("(no inbound trunks)")
        for t in resp.items:
            print(
                f"{t.sip_trunk_id}  name={t.name!r}  numbers={list(t.numbers)}  "
                f"allowed_addresses={list(t.allowed_addresses)}"
            )


async def cmd_create_inbound_trunk(args: argparse.Namespace) -> None:
    """Accept calls TO our DIDs, and only from Zadarma.

    No auth_username/password, for the same reason as the outbound trunk: the
    number is already routed to our IP-authorised SIP login on Zadarma's side,
    so the source address IS the credential. `allowed_addresses` is therefore
    not a nicety — without it this trunk answers an INVITE from anyone who
    finds port 5060, and every one of those calls would start a paid realtime
    model session.
    """
    numbers = args.numbers or ALL_DIDS
    if not numbers:
        raise SystemExit(
            "no numbers: pass --numbers +370…,+44… or set ZVONOK_OWNED_CALLER_IDS"
        )

    async with lkapi() as lk:
        existing = await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
        for t in existing.items:
            if t.name == args.name:
                print(f"inbound trunk {args.name!r} already exists: {t.sip_trunk_id}")
                # Say so loudly rather than returning quietly. A trunk created
                # against an older network list keeps rejecting INVITEs from the
                # ranges it has never heard of, and the symptom is a number that
                # does not ring — no error anywhere, because refusing an INVITE
                # from an unlisted address is this trunk working as designed.
                stale = sorted(set(ZADARMA_SIP_NETS) - set(t.allowed_addresses))
                if stale:
                    print(f"  ⚠ its allowed_addresses are {list(t.allowed_addresses)}")
                    print(f"  ⚠ missing: {', '.join(stale)}")
                    print("  run:  ./lkctl.sh sync-inbound-trunk")
                return

        trunk = api.SIPInboundTrunkInfo(
            name=args.name,
            numbers=numbers,
            allowed_addresses=ZADARMA_SIP_NETS,
        )
        # An inbound call is someone else's decision to spend our money, so the
        # ceiling is set on the trunk itself rather than left to the agent: it
        # holds even if the agent process dies mid-call.
        trunk.max_call_duration.FromSeconds(args.max_duration)
        trunk.ringing_timeout.FromSeconds(20)
        resp = await lk.sip.create_inbound_trunk(
            api.CreateSIPInboundTrunkRequest(trunk=trunk)
        )
        print(f"created inbound trunk: {resp.sip_trunk_id}")
        print(f"  numbers: {', '.join(numbers)}")
        print(f"  accepts INVITEs only from {', '.join(ZADARMA_SIP_NETS)}")
        print()
        print("next:  ./lkctl.sh create-inbound-rule --trunk " + resp.sip_trunk_id)


async def cmd_sync_inbound_trunk(args: argparse.Namespace) -> None:
    """Bring an existing inbound trunk's allowed_addresses up to date.

    Separate from create because the interesting case is a trunk that already
    exists and is quietly refusing calls. `numbers` is deliberately left alone —
    this only touches the network allowlist, so it cannot accidentally widen
    which DIDs we answer for.

    `--max-duration` is here because the trunk's own ceiling is the only thing
    bounding an inbound call that nothing answers. Until a secretary agent
    exists, the dispatch rule names a worker that never joins: livekit-sip still
    answers the INVITE, so the caller gets silence and we get billed for it, for
    the full ceiling. Keep it short until something is actually listening.
    """
    async with lkapi() as lk:
        resp = await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
        # A --name that matches nothing used to exit 0 in silence, which reads
        # exactly like "already up to date" — the worst possible confusion for a
        # command whose job is to fix a trunk that is refusing calls.
        matched = [t for t in resp.items if not args.name or t.name == args.name]
        if not matched:
            raise SystemExit(
                f"no inbound trunk named {args.name!r} "
                f"(have: {', '.join(t.name for t in resp.items) or 'none'})"
            )
        if args.name and len(matched) > 1:
            print(f"⚠ {len(matched)} trunks are named {args.name!r} — updating all")
        for t in matched:
            nets_ok = set(t.allowed_addresses) == set(ZADARMA_SIP_NETS)
            cap_ok = (
                args.max_duration is None
                or t.max_call_duration.seconds == args.max_duration
            )
            if nets_ok and cap_ok:
                print(f"{t.sip_trunk_id} ({t.name!r}) already up to date")
                continue
            print(f"{t.sip_trunk_id} ({t.name!r})")
            print(f"  was: {list(t.allowed_addresses)}"
                  f"  max_call_duration={t.max_call_duration.seconds}s")
            # ⚠ `update_inbound_trunk` REPLACES THE TRUNK ENTIRELY — it is not a
            # patch, whatever the name suggests. Passing a fresh object with
            # only allowed_addresses set would silently drop `numbers` (so the
            # trunk answers for no DID at all) and both server-side fuses
            # (max_call_duration, ringing_timeout), which are the only thing
            # bounding a call if the agent dies mid-conversation. So: copy the
            # existing trunk and change the one field.
            updated = api.SIPInboundTrunkInfo()
            updated.CopyFrom(t)
            del updated.allowed_addresses[:]
            updated.allowed_addresses.extend(ZADARMA_SIP_NETS)
            if args.max_duration is not None:
                updated.max_call_duration.FromSeconds(args.max_duration)
            await lk.sip.update_inbound_trunk(t.sip_trunk_id, updated)
            print(f"  now: {ZADARMA_SIP_NETS}"
                  f"  max_call_duration={updated.max_call_duration.seconds}s")


async def cmd_dispatch_rules(_: argparse.Namespace) -> None:
    async with lkapi() as lk:
        resp = await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
        if not resp.items:
            print("(no dispatch rules)")
        for r in resp.items:
            agents = [a.agent_name for a in r.room_config.agents] if r.room_config else []
            print(
                f"{r.sip_dispatch_rule_id}  name={r.name!r}  trunks={list(r.trunk_ids)}  "
                f"agents={agents}"
            )


async def cmd_create_inbound_rule(args: argparse.Namespace) -> None:
    """Route an inbound call into its own room and put an agent in it.

    Individual (one room per call), not direct (everyone into one room): two
    people ringing our number at once must never land in the same conversation.
    """
    async with lkapi() as lk:
        existing = await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
        for r in existing.items:
            if r.name == args.name:
                print(f"dispatch rule {args.name!r} already exists: {r.sip_dispatch_rule_id}")
                return

        rule = api.SIPDispatchRuleInfo(
            name=args.name,
            trunk_ids=[args.trunk] if args.trunk else [],
            rule=api.SIPDispatchRule(
                dispatch_rule_individual=api.SIPDispatchRuleIndividual(
                    room_prefix="zvonok-in",
                )
            ),
            room_config=api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name=args.agent)]
            ),
        )
        resp = await lk.sip.create_dispatch_rule(
            api.CreateSIPDispatchRuleRequest(dispatch_rule=rule)
        )
        print(f"created dispatch rule: {resp.sip_dispatch_rule_id}")
        print(f"  inbound calls → room zvonok-in_* → agent {args.agent!r}")
        print()
        print(
            "⚠ nothing answers until a worker registered as "
            f"{args.agent!r} is running."
        )


async def cmd_dispatch(args: argparse.Namespace) -> None:
    job_id = args.job_id or f"p1-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    room = f"zvonok-{job_id}"
    metadata = json.dumps({
        "number": args.number,
        "goal": args.goal,
        "language": args.language,
        "caller_id": args.caller_id,
        "job_id": job_id,
        "max_duration_seconds": args.max_duration,
    })

    print(f"job:       {job_id}")
    print(f"room:      {room}")
    print(f"to:        {args.number}")
    print(f"caller id: {args.caller_id}")
    print(f"goal:      {args.goal}")
    print()

    async with lkapi() as lk:
        resp = await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="zvonok-caller", room=room, metadata=metadata
            )
        )
        print(f"dispatched: {resp.id}")


async def cmd_rooms(_: argparse.Namespace) -> None:
    async with lkapi() as lk:
        resp = await lk.room.list_rooms(api.ListRoomsRequest())
        if not resp.rooms:
            print("(no active rooms — nothing in flight)")
        for r in resp.rooms:
            print(f"{r.name}  participants={r.num_participants}")


async def cmd_hangup(args: argparse.Namespace) -> None:
    """Kill a live call. Deleting the room drops the SIP leg — the same thing
    end_call does, available when the agent is stuck and the meter is running."""
    async with lkapi() as lk:
        resp = await lk.room.list_rooms(api.ListRoomsRequest())
        targets = [r.name for r in resp.rooms if args.room in ("all", r.name)]
        if not targets:
            print(f"no room matching {args.room!r}")
            return
        for name in targets:
            await lk.room.delete_room(api.DeleteRoomRequest(room=name))
            print(f"hung up: {name}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("trunks", help="list outbound trunks")

    ct = sub.add_parser("create-trunk", help="create the Zadarma outbound trunk")
    ct.add_argument("--name", default="zadarma")

    sub.add_parser("inbound-trunks", help="list inbound trunks")
    si = sub.add_parser(
        "sync-inbound-trunk", help="update allowed_addresses to Zadarma's current list"
    )
    si.add_argument("--name", default=None, help="only this trunk; omit for all")
    si.add_argument(
        "--max-duration", type=int, default=None,
        help="also set the trunk's call ceiling, in seconds. Keep it short "
             "(45-60) while no agent answers inbound: the INVITE is accepted "
             "either way and silence bills the same as a conversation.",
    )

    ci = sub.add_parser("create-inbound-trunk", help="accept calls to our DIDs")
    ci.add_argument("--name", default="zadarma-in")
    ci.add_argument(
        "--numbers", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
        help="comma-separated E.164; defaults to ZVONOK_OWNED_CALLER_IDS",
    )
    ci.add_argument("--max-duration", dest="max_duration", type=int, default=600)

    sub.add_parser("dispatch-rules", help="list SIP dispatch rules")

    cr = sub.add_parser("create-inbound-rule", help="route inbound calls to an agent")
    cr.add_argument("--name", default="zvonok-inbound")
    cr.add_argument("--trunk", default=None, help="inbound trunk id; omit for all")
    cr.add_argument("--agent", default="zvonok-secretary")

    d = sub.add_parser("dispatch", help="place a call")
    d.add_argument("--number", required=True, help="destination in E.164")
    d.add_argument("--goal", required=True)
    d.add_argument("--language", default="en", choices=["en", "ru", "es", "pl"])
    d.add_argument("--caller-id", dest="caller_id", default=DEFAULT_CALLER_ID)
    d.add_argument("--max-duration", dest="max_duration", type=int, default=300)
    d.add_argument("--job-id", dest="job_id", default=None)

    sub.add_parser("rooms", help="list live calls")

    h = sub.add_parser("hangup", help="force-end a live call")
    h.add_argument("room", nargs="?", default="all", help="room name, or 'all'")

    args = p.parse_args()
    fn = {
        "trunks": cmd_trunks,
        "create-trunk": cmd_create_trunk,
        "inbound-trunks": cmd_inbound_trunks,
        "sync-inbound-trunk": cmd_sync_inbound_trunk,
        "create-inbound-trunk": cmd_create_inbound_trunk,
        "dispatch-rules": cmd_dispatch_rules,
        "create-inbound-rule": cmd_create_inbound_rule,
        "dispatch": cmd_dispatch,
        "rooms": cmd_rooms,
        "hangup": cmd_hangup,
    }[args.cmd]
    try:
        asyncio.run(fn(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
