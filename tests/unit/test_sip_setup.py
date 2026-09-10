"""Inbound SIP provisioning (offline: livekit-api calls are stubbed)."""

from __future__ import annotations

import pytest

pytest.importorskip("livekit.api")

from voiceagent.channels import sip
from voiceagent.settings import SettingsError, load_settings


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path):
    yaml_file = tmp_path / "agents.yaml"
    yaml_file.write_text(
        """
agents:
  - name: sales
    prompt: { text: Hi. }
    llm: { provider: mock }
    stt: { provider: mock }
    tts: { provider: mock }
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 36)
    monkeypatch.setenv("VOICEAGENT_AGENTS_FILE", str(yaml_file))
    return load_settings(require_livekit=True)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls: dict = {}

    async def fake_trunk(lk, **kwargs):
        calls["trunk"] = kwargs
        return "ST_trunk123"

    async def fake_rule(lk, **kwargs):
        calls["rule"] = kwargs
        return "SDR_rule456"

    monkeypatch.setattr(sip, "create_inbound_trunk", fake_trunk)
    monkeypatch.setattr(sip, "create_inbound_dispatch_rule", fake_rule)
    return calls


async def test_setup_inbound_creates_trunk_and_rule(settings, captured: dict) -> None:
    trunk_id, rule_id = await sip.setup_inbound(
        settings, numbers=["+15551234567"], agent_id="sales"
    )
    assert trunk_id == "ST_trunk123"
    assert rule_id == "SDR_rule456"

    assert captured["trunk"]["numbers"] == ["+15551234567"]
    rule = captured["rule"]
    assert rule["trunk_ids"] == ["ST_trunk123"]
    assert rule["room_prefix"] == "sip-"
    assert rule["worker_name"] == "voiceagent"
    # metadata pins the agent and the channel; the worker fills the rest.
    assert '"agent_id":"sales"' in rule["metadata"].to_json()
    assert '"channel":"sip"' in rule["metadata"].to_json()


async def test_setup_inbound_rejects_unknown_agent(settings, captured: dict) -> None:
    with pytest.raises(SettingsError, match="unknown agent"):
        await sip.setup_inbound(settings, numbers=["+1555"], agent_id="ghost")
    assert "trunk" not in captured  # nothing provisioned on validation failure


async def test_setup_inbound_requires_a_number(settings, captured: dict) -> None:
    with pytest.raises(SettingsError, match="phone number"):
        await sip.setup_inbound(settings, numbers=[], agent_id="sales")


def test_cli_parses_and_provisions(monkeypatch: pytest.MonkeyPatch, settings) -> None:
    seen: dict = {}

    async def fake_setup(_settings, **kwargs):
        seen.update(kwargs)
        return "ST_x", "SDR_y"

    monkeypatch.setattr(sip, "load_settings", lambda **_: settings)
    monkeypatch.setattr(sip, "setup_inbound", fake_setup)

    rc = sip.main(
        ["setup", "--numbers", "+1555,+1666", "--agent", "sales", "--room-prefix", "pstn-"]
    )
    assert rc == 0
    assert seen["numbers"] == ["+1555", "+1666"]
    assert seen["agent_id"] == "sales"
    assert seen["room_prefix"] == "pstn-"
