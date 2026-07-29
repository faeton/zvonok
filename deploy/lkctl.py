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

    d = sub.add_parser("dispatch", help="place a call")
    d.add_argument("--number", required=True, help="destination in E.164")
    d.add_argument("--goal", required=True)
    d.add_argument("--language", default="en", choices=["en", "ru", "es"])
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
