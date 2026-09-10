"""WhatsApp Calling channel: standalone service bridging Meta WebRTC to a room.

The engine-neutral pieces (SDP munge, Meta client, webhook parsing) live here;
the aiortc<->rtc.Room media bridge lives in voiceagent.livekit.room_audio.
"""

from __future__ import annotations

from voiceagent.channels.whatsapp.sdp import munge_sdp_for_meta

__all__ = ["munge_sdp_for_meta"]
