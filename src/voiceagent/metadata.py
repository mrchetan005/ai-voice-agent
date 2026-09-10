"""Session dispatch metadata: the wire format between channels and workers.

A channel (browser token, SIP dispatch rule, WhatsApp bridge) attaches this
JSON to the agent dispatch; the worker parses it in the job entrypoint.
Parsing is tolerant by design — a malformed payload degrades to defaults
instead of refusing the session.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("voiceagent")

_STR_FIELDS = frozenset({"tenant_id", "agent_id", "session_id", "channel"})
_OPT_STR_FIELDS = frozenset({"user_id", "call_id", "memory_key", "language"})
_KNOWN_FIELDS = _STR_FIELDS | _OPT_STR_FIELDS | {"record", "v", "prompt_vars"}


def _field_ok(key: str, value: object) -> bool:
    if key in _STR_FIELDS:
        return isinstance(value, str)
    if key in _OPT_STR_FIELDS:
        return value is None or isinstance(value, str)
    if key == "record":
        return isinstance(value, bool)
    if key == "v":
        return isinstance(value, int) and not isinstance(value, bool)
    return False


@dataclass(slots=True)
class SessionMetadata:
    v: int = 1
    tenant_id: str = "default"
    agent_id: str = ""
    session_id: str = ""
    channel: str = "browser"  # "browser" | "sip" | "whatsapp" | "test"
    user_id: str | None = None
    call_id: str | None = None
    prompt_vars: dict[str, str] = field(default_factory=dict)
    memory_key: str | None = None
    record: bool = False
    language: str | None = None  # whitelisted runtime override

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | None) -> SessionMetadata:
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError(f"expected object, got {type(data).__name__}")
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("unparseable session metadata (%s); using defaults", exc)
            return cls()
        kwargs: dict[str, object] = {}
        for key, value in data.items():
            if key == "prompt_vars" and isinstance(value, dict):
                kwargs[key] = {str(k): str(v) for k, v in value.items()}
            elif _field_ok(key, value):
                kwargs[key] = value
            elif key in _KNOWN_FIELDS:
                logger.warning("session metadata field %r has wrong type; using default", key)
        return cls(**kwargs)  # type: ignore[arg-type]
