"""Recording via LiveKit Egress: audio-only OGG to S3-compatible storage.

Fire-and-forget from the worker: a failed recording start logs and returns
None — it must never affect the realtime path. Egress ends automatically
when the room closes.
"""

from __future__ import annotations

import logging

from livekit import api

from voiceagent.events import SessionIDs
from voiceagent.settings import RecordingSettings

logger = logging.getLogger("voiceagent")


def recording_filepath(rec: RecordingSettings, ids: SessionIDs) -> str:
    return f"{rec.prefix}/{ids.tenant_id}/{ids.agent_id}/{ids.session_id}.ogg"


async def start_room_recording(
    lkapi: api.LiveKitAPI,
    *,
    room_name: str,
    rec: RecordingSettings,
    ids: SessionIDs,
) -> str | None:
    """Start an audio-only room-composite egress; returns the storage path."""
    filepath = recording_filepath(rec, ids)
    request = api.RoomCompositeEgressRequest(
        room_name=room_name,
        audio_only=True,
        file_outputs=[
            api.EncodedFileOutput(
                file_type=api.EncodedFileType.OGG,
                filepath=filepath,
                s3=api.S3Upload(
                    access_key=rec.access_key,
                    secret=rec.secret_key,
                    region=rec.region,
                    endpoint=rec.s3_endpoint,
                    bucket=rec.bucket,
                    force_path_style=True,  # MinIO and most S3-compatibles
                ),
            )
        ],
    )
    try:
        egress = await lkapi.egress.start_room_composite_egress(request)
        logger.info("recording started for %s (egress %s)", ids.session_id, egress.egress_id)
        return filepath
    except Exception as exc:
        logger.warning("recording start failed for %s: %s", ids.session_id, exc)
        return None
