"""WhatsApp webhook parsing + verification (offline, recorded fixtures)."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from voiceagent.channels.whatsapp.webhook import (
    EventRouter,
    SessionConflict,
    verify_signature,
    verify_subscription,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "meta_webhooks"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_verify_subscription() -> None:
    assert verify_subscription("subscribe", "tok", "tok") is True
    assert verify_subscription("subscribe", "wrong", "tok") is False
    assert verify_subscription("unsubscribe", "tok", "tok") is False
    assert verify_subscription("subscribe", "", "") is False  # no configured token


def test_verify_signature() -> None:
    secret, body = "s3cr3t", b'{"a":1}'
    good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, good, secret) is True
    assert verify_signature(body, "sha256=deadbeef", secret) is False
    assert verify_signature(body, None, secret) is False
    assert verify_signature(body, None, "") is True  # unconfigured secret = skip


def test_inbound_offer_routes_to_incoming_calls() -> None:
    router = EventRouter()
    router.dispatch(_load("inbound_offer.json"))
    event = router.incoming_calls.get_nowait()
    assert event.sdp_type == "offer"
    assert event.call_id == "wacid.INBOUND_ABC123"
    assert event.from_number == "919876543210"
    assert "opus" in event.sdp


def test_outbound_answer_routes_to_session() -> None:
    router = EventRouter()
    session = router.open_call("919876543210", "outbound")
    session.call_id = "wacid.OUT_XYZ789"
    router.dispatch(_load("outbound_answer.json"))
    answer = session.answers.get_nowait()
    assert answer.sdp_type == "answer"
    assert "opus" in answer.sdp
    assert router.incoming_calls.empty()


def test_terminate_matches_strictly_by_call_id() -> None:
    router = EventRouter()
    session = router.open_call("919876543210", "outbound")
    session.call_id = "wacid.OUT_XYZ789"
    router.dispatch(_load("call_terminated.json"))
    assert session.ended.is_set()


def test_terminate_for_other_call_id_does_not_end_live_call() -> None:
    """Meta redelivers a previous call's terminate late; it must not kill the
    live call (the bug the strict matcher exists to prevent)."""
    router = EventRouter()
    session = router.open_call("919876543210", "outbound")
    session.call_id = "wacid.SOME_OTHER_CALL"  # live call has a different id
    router.dispatch(_load("call_terminated.json"))
    assert not session.ended.is_set()


def test_permission_reply_routes_to_session() -> None:
    router = EventRouter()
    session = router.open_call("919876543210", "outbound")
    router.dispatch(_load("permission_accept.json"))
    assert session.permission.get_nowait() is True


def test_open_call_conflict() -> None:
    router = EventRouter()
    router.open_call("+919876543210", "outbound")
    with pytest.raises(SessionConflict):
        router.open_call("919876543210", "inbound")  # same peer, "+" normalized
