"""WhatsApp service boot + webhook HTTP surface (offline, no Meta, no LiveKit)."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("aiortc")
pytest.importorskip("livekit.agents")

import httpx

from voiceagent.channels.whatsapp.service import create_app
from voiceagent.settings import load_settings

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "meta_webhooks"
_VERIFY_TOKEN = "verify-me"
_APP_SECRET = "app-secret"


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    yaml_file = tmp_path / "agents.yaml"
    yaml_file.write_text(
        """
agents:
  - name: mock-demo
    prompt: { text: Echo. }
    llm: { provider: mock }
    stt: { provider: mock }
    tts: { provider: mock }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 36)
    monkeypatch.setenv("LIVEKIT_URL", "ws://localhost:7880")
    monkeypatch.setenv("VOICEAGENT_AGENTS_FILE", str(yaml_file))
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", _VERIFY_TOKEN)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", _APP_SECRET)
    app = create_app(load_settings())
    # ASGITransport does not run lifespan; enter it manually.
    async with (
        httpx.ASGITransport(app=app) as transport,
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
    ):
        yield http_client


async def test_healthz(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).status_code == 200


async def test_verify_handshake_echoes_challenge(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": _VERIFY_TOKEN,
            "hub.challenge": "nonce-42",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "nonce-42"


async def test_verify_handshake_wrong_token_403(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
    )
    assert resp.status_code == 403


async def test_post_bad_signature_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/webhooks/whatsapp",
        content=b'{"object":"whatsapp_business_account"}',
        headers={"X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


async def test_post_good_signature_accepted(client: httpx.AsyncClient) -> None:
    # A permission reply for a peer with no open session is a safe no-op — no
    # Meta/LiveKit calls — so this exercises the signed-POST path end to end.
    body = (_FIXTURES / "permission_accept.json").read_bytes()
    sig = "sha256=" + hmac.new(_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = await client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "received"}


async def test_outbound_requires_bearer(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/whatsapp/calls",
        json={"agent_id": "mock-demo", "to_number": "+919876543210"},
    )
    assert resp.status_code == 401


def test_permission_fixture_is_wellformed() -> None:
    # Guards against the fixture drifting out of the shape the POST test relies on.
    payload = json.loads((_FIXTURES / "permission_accept.json").read_text(encoding="utf-8"))
    assert payload["object"] == "whatsapp_business_account"
