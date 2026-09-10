"""Meta WhatsApp Cloud API client — the Business Calling surface.

Business-initiated call flow (verified on `main`, Aug 2026):
  1. send_permission_request()  -> user taps Accept on WhatsApp
  2. webhook delivers the acceptance (see webhook.py)
  3. initiate_call(sdp_offer)   -> POST /{PHONE_NUMBER_ID}/calls action=connect
  4. SDP answer arrives on the `calls` webhook; WebRTC media flows
  5. terminate_call(call_id)

Inbound is the mirror: the caller's webhook carries an SDP OFFER, answered
with pre_accept_call then accept_call (same munged SDP).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from voiceagent.settings import WhatsAppSettings

logger = logging.getLogger("voiceagent")


class WhatsAppClient:
    def __init__(self, wa: WhatsAppSettings) -> None:
        missing = wa.require_credentials()
        if missing:
            raise RuntimeError(f"WhatsApp client needs: {', '.join(missing)}")
        self.phone_number_id = wa.phone_number_id
        self._http = httpx.AsyncClient(
            base_url=f"https://graph.facebook.com/{wa.graph_version}",
            headers={"Authorization": f"Bearer {wa.access_token}"},
            timeout=30.0,
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post(path, json=payload)
        if resp.status_code >= 400:
            # Meta packs the reason into error.message/error_data; surface it —
            # 138017 (calling not enabled), 138008 (bad SDP), 131030 (recipient
            # not in allowlist) all land here.
            logger.error("graph %s -> %s: %s", path, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()

    async def send_text(self, to: str, body: str) -> dict[str, Any]:
        return await self._post(
            f"/{self.phone_number_id}/messages",
            {"messaging_product": "whatsapp", "to": to, "type": "text",
             "text": {"body": body}},
        )

    async def send_permission_request(self, to: str, reason: str) -> dict[str, Any]:
        """Interactive call-permission request (works inside a 24 h session;
        outside one, Meta requires an approved template with a
        VOICE_CALL_REQUEST button)."""
        return await self._post(
            f"/{self.phone_number_id}/messages",
            {"messaging_product": "whatsapp", "to": to, "type": "interactive",
             "interactive": {"type": "call_permission_request",
                             "body": {"text": reason},
                             "action": {"name": "call_permission_request"}}},
        )

    async def enable_calling(self) -> dict[str, Any]:
        """Idempotent: flip the Calling API on for this number and show the
        in-chat call button so users can call us (inbound)."""
        return await self._post(
            f"/{self.phone_number_id}/settings",
            {"calling": {"status": "ENABLED",
                         "callback_permission_status": "ENABLED",
                         "call_icon_visibility": "DEFAULT"}},
        )

    async def get_settings(self) -> dict[str, Any]:
        resp = await self._http.get(f"/{self.phone_number_id}/settings")
        resp.raise_for_status()
        return resp.json()

    async def initiate_call(self, to: str, sdp_offer: str) -> dict[str, Any]:
        # The /calls allowlist matches EXACTLY and stores numbers without "+"
        # (wa_id form): "+91..." gets 131030 while "91..." passes. Messaging
        # normalizes both; calling does not.
        return await self._post(
            f"/{self.phone_number_id}/calls",
            {"messaging_product": "whatsapp", "to": to.lstrip("+"), "action": "connect",
             "session": {"sdp_type": "offer", "sdp": sdp_offer}},
        )

    async def pre_accept_call(self, call_id: str, sdp_answer: str) -> dict[str, Any]:
        """Inbound: pre-accept speeds media setup before the full accept."""
        return await self._post(
            f"/{self.phone_number_id}/calls",
            {"messaging_product": "whatsapp", "call_id": call_id, "action": "pre_accept",
             "session": {"sdp_type": "answer", "sdp": sdp_answer}},
        )

    async def accept_call(self, call_id: str, sdp_answer: str) -> dict[str, Any]:
        return await self._post(
            f"/{self.phone_number_id}/calls",
            {"messaging_product": "whatsapp", "call_id": call_id, "action": "accept",
             "session": {"sdp_type": "answer", "sdp": sdp_answer}},
        )

    async def terminate_call(self, call_id: str) -> dict[str, Any]:
        return await self._post(
            f"/{self.phone_number_id}/calls",
            {"messaging_product": "whatsapp", "call_id": call_id, "action": "terminate"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()
