"""Platform API contracts — offline (stores disabled, no LiveKit server)."""

from __future__ import annotations

import pytest

pytest.importorskip("livekit.agents")
pytest.importorskip("fastapi")

import httpx
import jwt

from voiceagent.platform.app import create_app
from voiceagent.settings import load_settings

SECRET = "s" * 36
AUTH = {"Authorization": "Bearer devtoken"}


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
    monkeypatch.setenv("PLATFORM_API_TOKEN", "devtoken")
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://localhost:7880")
    monkeypatch.setenv("VOICEAGENT_AGENTS_FILE", str(yaml_file))
    app = create_app(load_settings())
    # ASGITransport does not run lifespan; enter it manually.
    async with (
        httpx.ASGITransport(app=app) as transport,
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
    ):
        yield http_client


async def test_healthz_is_public(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200


async def test_auth_required_and_wrong_token_rejected(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/agents")).status_code == 401
    resp = await client.get("/v1/agents", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


async def test_agents_catalog(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/agents", headers=AUTH)
    assert resp.status_code == 200
    (entry,) = resp.json()
    assert entry["agent_id"] == "mock-demo"
    assert entry["mode"] == "pipeline"
    assert entry["providers"]["llm"] == "mock"


async def test_create_session_mints_dispatching_token(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/sessions",
        headers=AUTH,
        json={"agent_id": "mock-demo", "prompt_vars": {"company": "Acme"}, "user_id": "u1"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["room"].startswith("va-mock-demo-")
    assert body["livekit_url"] == "ws://localhost:7880"

    claims = jwt.decode(body["token"], SECRET, algorithms=["HS256"], options={"verify_aud": False})
    assert claims["video"]["room"] == body["room"]
    assert claims["video"]["roomJoin"] is True
    room_config = claims.get("roomConfig") or claims.get("room_config")
    assert room_config is not None, f"agent dispatch missing from token claims: {claims.keys()}"
    (agent_dispatch,) = room_config["agents"]
    assert agent_dispatch["agentName"] == "voiceagent"
    assert '"agent_id":"mock-demo"' in agent_dispatch["metadata"]
    assert '"company":"Acme"' in agent_dispatch["metadata"]


async def test_create_session_unknown_agent_404(client: httpx.AsyncClient) -> None:
    resp = await client.post("/v1/sessions", headers=AUTH, json={"agent_id": "nope"})
    assert resp.status_code == 404


async def test_get_unknown_session_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/sessions/doesnotexist", headers=AUTH)
    assert resp.status_code == 404


async def test_sip_call_requires_trunk(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/calls/sip", headers=AUTH, json={"agent_id": "mock-demo", "to_number": "+155"}
    )
    assert resp.status_code == 400
    assert "trunk" in resp.json()["detail"]
