"""Thin wrappers over livekit-api: tokens, dispatch, SIP — used by channels
and the platform control plane so they never touch livekit types directly."""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from collections.abc import AsyncIterator

from livekit import api

from voiceagent.metadata import SessionMetadata
from voiceagent.settings import LiveKitSettings

logger = logging.getLogger("voiceagent")


def _http_url(url: str) -> str:
    if url.startswith("ws://"):
        return "http://" + url[len("ws://") :]
    if url.startswith("wss://"):
        return "https://" + url[len("wss://") :]
    return url


@contextlib.asynccontextmanager
async def livekit_api(lk: LiveKitSettings) -> AsyncIterator[api.LiveKitAPI]:
    client = api.LiveKitAPI(url=_http_url(lk.url), api_key=lk.api_key, api_secret=lk.api_secret)
    try:
        yield client
    finally:
        await client.aclose()


def mint_join_token(
    lk: LiveKitSettings,
    *,
    room: str,
    identity: str,
    name: str | None = None,
    worker_name: str | None = None,
    metadata: SessionMetadata | None = None,
    ttl_s: int = 3600,
    can_publish: bool = True,
    can_subscribe: bool = True,
) -> str:
    """Room-join JWT; when worker_name is given, the token also carries the
    agent dispatch so the worker joins as soon as the participant connects."""
    token = (
        api.AccessToken(lk.api_key, lk.api_secret)
        .with_identity(identity)
        .with_name(name or identity)
        .with_ttl(dt.timedelta(seconds=ttl_s))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=can_publish,
                can_subscribe=can_subscribe,
            )
        )
    )
    if worker_name:
        token = token.with_room_config(
            api.RoomConfiguration(
                agents=[
                    api.RoomAgentDispatch(
                        agent_name=worker_name,
                        metadata=(metadata or SessionMetadata()).to_json(),
                    )
                ],
            )
        )
    return token.to_jwt()


async def dispatch_agent(
    lk: LiveKitSettings,
    *,
    room: str,
    worker_name: str,
    metadata: SessionMetadata,
) -> str:
    """Server-side dispatch: send a worker into a room (SIP/WhatsApp/outbound)."""
    async with livekit_api(lk) as lkapi:
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=worker_name,
                room=room,
                metadata=metadata.to_json(),
            )
        )
        return dispatch.id


async def create_sip_outbound(
    lk: LiveKitSettings,
    *,
    trunk_id: str,
    to_number: str,
    room: str,
    identity: str = "sip-callee",
    wait_until_answered: bool = False,
) -> str:
    """Dial out through a SIP trunk and drop the callee into the room."""
    async with livekit_api(lk) as lkapi:
        participant = await lkapi.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=trunk_id,
                sip_call_to=to_number,
                room_name=room,
                participant_identity=identity,
                wait_until_answered=wait_until_answered,
            )
        )
        return participant.participant_id


async def delete_room(lk: LiveKitSettings, room: str) -> None:
    async with livekit_api(lk) as lkapi:
        with contextlib.suppress(Exception):
            await lkapi.room.delete_room(api.DeleteRoomRequest(room=room))
