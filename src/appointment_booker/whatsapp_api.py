"""Meta WhatsApp Cloud API client: messaging + Business Calling API.

Business-initiated call flow (verified Aug 2026):
  1. send_permission_request()  -> user taps Accept on WhatsApp
  2. webhook delivers the acceptance (see webhooks.py)
  3. initiate_call(sdp_offer)   -> POST /{PHONE_NUMBER_ID}/calls action=connect
  4. SDP answer arrives on the `calls` webhook; WebRTC media flows
  5. terminate_call(call_id)

Sanity check (sends a real WhatsApp text to WHATSAPP_RECIPIENT):
    uv run --env-file .env python -m appointment_booker.whatsapp_api
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import httpx

logger = logging.getLogger("appointment_booker")

GRAPH = "https://graph.facebook.com/v24.0"


class WhatsAppClient:
    def __init__(
        self,
        phone_number_id: str | None = None,
        access_token: str | None = None,
    ) -> None:
        self.phone_number_id = phone_number_id or os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or ""
        token = access_token or os.environ.get("WHATSAPP_ACCESS_TOKEN") or ""
        if not self.phone_number_id or not token:
            raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_ACCESS_TOKEN missing")
        self._http = httpx.AsyncClient(
            base_url=GRAPH,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post(path, json=payload)
        if resp.status_code >= 400:
            # Meta packs the reason into error.message/error_data; surface it —
            # "calling not enabled" and "no permission" both land here.
            logger.error("graph %s -> %s: %s", path, resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()

    # -- messaging ---------------------------------------------------------

    async def send_text(self, to: str, body: str) -> dict[str, Any]:
        return await self._post(f"/{self.phone_number_id}/messages", {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        })

    async def send_permission_request(self, to: str, reason: str) -> dict[str, Any]:
        """Interactive call-permission request (works inside a 24 h session;
        outside one, Meta requires an approved template with a
        VOICE_CALL_REQUEST button — have the user message the number first
        during development)."""
        return await self._post(f"/{self.phone_number_id}/messages", {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "call_permission_request",
                "body": {"text": reason},
                "action": {"name": "call_permission_request"},
            },
        })

    # -- calling -----------------------------------------------------------

    async def enable_calling(self) -> dict[str, Any]:
        """Idempotent: flips the Calling API on for this number.

        callback_permission_status additionally lets missed/connected calls
        trigger a call-permission prompt on the user's side (per the
        call-settings doc) — useful, not required.
        """
        return await self._post(f"/{self.phone_number_id}/settings", {
            "calling": {
                "status": "ENABLED",
                "callback_permission_status": "ENABLED",
                # Show the call button in chat so users can call US (inbound).
                "call_icon_visibility": "DEFAULT",
            },
        })

    async def get_settings(self) -> dict[str, Any]:
        """Read current calling settings; the response's
        restrictions.restrictions_list reveals RESTRICTED_BUSINESS_INITIATED_CALLING
        if Meta has gated this number (needs whatsapp_business_management)."""
        resp = await self._http.get(f"/{self.phone_number_id}/settings")
        resp.raise_for_status()
        return resp.json()

    async def initiate_call(self, to: str, sdp_offer: str) -> dict[str, Any]:
        # The /calls endpoint matches the test-number allowlist EXACTLY and
        # stores entries without "+" (wa_id form); "+91..." gets 131030 while
        # "91..." passes. Messaging normalizes both — calling doesn't.
        return await self._post(f"/{self.phone_number_id}/calls", {
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "action": "connect",
            "session": {"sdp_type": "offer", "sdp": sdp_offer},
        })

    async def pre_accept_call(self, call_id: str, sdp_answer: str) -> dict[str, Any]:
        """Inbound: pre-accept speeds up media setup before the full accept."""
        return await self._post(f"/{self.phone_number_id}/calls", {
            "messaging_product": "whatsapp",
            "call_id": call_id,
            "action": "pre_accept",
            "session": {"sdp_type": "answer", "sdp": sdp_answer},
        })

    async def accept_call(self, call_id: str, sdp_answer: str) -> dict[str, Any]:
        return await self._post(f"/{self.phone_number_id}/calls", {
            "messaging_product": "whatsapp",
            "call_id": call_id,
            "action": "accept",
            "session": {"sdp_type": "answer", "sdp": sdp_answer},
        })

    async def terminate_call(self, call_id: str) -> dict[str, Any]:
        return await self._post(f"/{self.phone_number_id}/calls", {
            "messaging_product": "whatsapp",
            "call_id": call_id,
            "action": "terminate",
        })

    async def aclose(self) -> None:
        await self._http.aclose()


async def _self_check() -> int:
    import json

    wa = WhatsAppClient()
    to = os.environ["WHATSAPP_RECIPIENT"]
    result = await wa.send_text(to, "voiceagent appointment-booker: connectivity check ✅")
    print(f"[ok] text sent to {to}: {result.get('messages', [{}])[0].get('id', '?')}")
    try:
        settings = await wa.get_settings()
        print("[ok] settings:")
        print(json.dumps(settings, indent=2))
        restrictions = (settings.get("restrictions") or {}).get("restrictions_list") or []
        if restrictions:
            print(f"[!!] RESTRICTIONS ACTIVE: {restrictions}")
    except Exception as exc:
        print(f"[--] settings read failed (token may lack whatsapp_business_management): {exc}")
    await wa.aclose()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_self_check()))
