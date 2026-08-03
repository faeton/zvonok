"""LiveKit control plane: dispatch a job, see what is in flight, kill a call.

Deliberately thin. Phase 2 keeps DIALLING IN THE AGENT (BRIEF §9 phase-2 trap 1):
call-api owns job state, the agent owns the SIP participant. That split is what
makes "start the AgentSession before dialling" possible — the trick that stops us
missing the callee's first words — and it means there is exactly one component
that can create a paid PSTN leg. Half-migrating this would produce double-dials
and orphan rooms, which is precisely how idempotency breaks at the root.

Consequence worth stating plainly: **the LiveKit room is the source of truth for
"a call is in flight"**, and the jobs table is a record of what we asked for.
When the two disagree, the room wins.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from livekit import api

from .config import settings

logger = logging.getLogger("zvonok.dispatch")


def _lk() -> api.LiveKitAPI:
    return api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )


def room_name_for(job_id: str) -> str:
    return f"zvonok-{job_id}"


async def dispatch_call(
    job_id: str, metadata: dict[str, Any], agent_name: str
) -> tuple[str, str]:
    """Create the agent job. Returns (room_name, dispatch_id).

    The metadata carries call parameters only — never a credential. The agent
    reaches call-api using its own env-provided internal token, because dispatch
    metadata is persisted in LiveKit/Redis and shows up in logs.

    `agent_name` is how a multi-tenant deployment stays honest about that rule.
    Each tenant runs its own worker under its own name, holding its own Zadarma
    trunk and voice-brain key in its own process env; choosing the name here is
    the only tenant-specific thing that crosses this boundary, and it is not a
    secret. Route a job to the wrong name and the call goes out on the wrong
    account's trunk with the wrong number — which is why config warns when two
    tenants share one.
    """
    room = room_name_for(job_id)
    async with _lk() as lk:
        resp = await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=agent_name,
                room=room,
                metadata=json.dumps(metadata, ensure_ascii=False),
            )
        )
    logger.info(
        "dispatched job %s → room %s via %s (%s)", job_id, room, agent_name, resp.id
    )
    return room, resp.id


async def live_rooms() -> dict[str, int]:
    """{room_name: participant_count} for every room LiveKit currently has.

    This is the check that makes idempotency mean "did not dial twice" rather
    than merely "returned the same id" (BRIEF §9 phase-2 trap 2).
    """
    async with _lk() as lk:
        resp = await lk.room.list_rooms(api.ListRoomsRequest())
    return {r.name: r.num_participants for r in resp.rooms}


async def revoke_dispatch(room: str, dispatch_id: str | None) -> bool:
    """Withdraw an agent job that has not been picked up yet.

    Deleting the room is not enough to cancel a call that has not started.
    Between `create_dispatch` and the worker actually joining there is no room to
    delete, so a cancel in that window would mark the job terminal here and then
    watch the agent dial anyway a moment later — an outbound call that nothing
    in the system is expecting, and a slot the caps believe is free.
    """
    if not dispatch_id:
        return False
    async with _lk() as lk:
        try:
            await lk.agent_dispatch.delete_dispatch(
                api.DeleteAgentDispatchRequest(dispatch_id=dispatch_id, room=room)
            )
            return True
        except api.TwirpError as e:
            # Already claimed or already gone — the room delete is what matters.
            logger.info("could not revoke dispatch %s: %s", dispatch_id, e)
            return False


async def hangup(room: str) -> bool:
    """Delete a room, which drops the SIP leg. True if it was there to delete."""
    async with _lk() as lk:
        try:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room))
            return True
        except api.TwirpError as e:
            if getattr(e, "code", "") in ("not_found", "NotFound"):
                return False
            raise
