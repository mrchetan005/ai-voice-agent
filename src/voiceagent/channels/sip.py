"""SIP channel: one-time setup for inbound PSTN calls.

Inbound and outbound SIP both reuse the homogeneous worker fleet — there is
no SIP-specific worker. Outbound is a runtime call (``POST /v1/calls/sip``);
inbound needs two provisioning objects created once against a SIP provider's
trunk:

  1. an *inbound trunk* that accepts calls to your DID(s), and
  2. a *dispatch rule* that drops each caller into their own room and dispatches
     the agent fleet with ``channel="sip"`` metadata.

Run it once after pointing your SIP provider (Twilio, Telnyx, a self-hosted
softswitch, …) at the LiveKit SIP service:

    python -m voiceagent.channels.sip setup \\
        --numbers +15551234567 --agent sales-pipeline

The livekit-api calls live in :mod:`voiceagent.livekit.api` so this module
stays engine-neutral (the layering guard forbids importing ``livekit`` here).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from voiceagent.agent import load_agents
from voiceagent.livekit.api import create_inbound_dispatch_rule, create_inbound_trunk
from voiceagent.metadata import SessionMetadata
from voiceagent.settings import Settings, SettingsError, load_settings


async def setup_inbound(
    settings: Settings,
    *,
    numbers: list[str],
    agent_id: str,
    room_prefix: str = "sip-",
    trunk_name: str = "voiceagent-inbound",
    rule_name: str = "voiceagent-inbound-rule",
    allowed_addresses: list[str] | None = None,
    auth_username: str = "",
    auth_password: str = "",
) -> tuple[str, str]:
    """Provision inbound SIP: create a trunk for `numbers` and a dispatch rule
    that routes callers into per-call rooms served by `agent_id`.

    Returns ``(trunk_id, dispatch_rule_id)``.
    """
    known = {a.name for a in load_agents(settings)}
    if known and agent_id not in known:
        raise SettingsError(f"unknown agent {agent_id!r}; known agents: {sorted(known)}")
    if not numbers:
        raise SettingsError("at least one phone number is required (--numbers)")

    trunk_id = await create_inbound_trunk(
        settings.livekit,
        name=trunk_name,
        numbers=numbers,
        allowed_addresses=allowed_addresses,
        auth_username=auth_username,
        auth_password=auth_password,
    )
    # Only agent_id + channel are known at provisioning time; the worker fills
    # session_id from the job and room_id from the room the rule creates.
    meta = SessionMetadata(agent_id=agent_id, channel="sip")
    rule_id = await create_inbound_dispatch_rule(
        settings.livekit,
        name=rule_name,
        trunk_ids=[trunk_id],
        room_prefix=room_prefix,
        worker_name=settings.agent_source.worker_name,
        metadata=meta,
    )
    return trunk_id, rule_id


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voiceagent.channels.sip")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="provision an inbound SIP trunk + dispatch rule")
    setup.add_argument(
        "--numbers",
        required=True,
        help="comma-separated DIDs to accept, e.g. +15551234567,+15557654321",
    )
    setup.add_argument("--agent", required=True, help="agent_id to serve inbound calls")
    setup.add_argument("--room-prefix", default="sip-", help="prefix for per-call rooms")
    setup.add_argument("--trunk-name", default="voiceagent-inbound")
    setup.add_argument("--rule-name", default="voiceagent-inbound-rule")
    setup.add_argument(
        "--allowed-addresses",
        default="",
        help="comma-separated CIDRs/IPs allowed to send INVITEs (default: any)",
    )
    setup.add_argument("--auth-username", default="")
    setup.add_argument("--auth-password", default="")
    return parser


def _split(csv: str) -> list[str]:
    return [item.strip() for item in csv.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = load_settings(require_livekit=True)
    if args.command == "setup":
        trunk_id, rule_id = asyncio.run(
            setup_inbound(
                settings,
                numbers=_split(args.numbers),
                agent_id=args.agent,
                room_prefix=args.room_prefix,
                trunk_name=args.trunk_name,
                rule_name=args.rule_name,
                allowed_addresses=_split(args.allowed_addresses) or None,
                auth_username=args.auth_username,
                auth_password=args.auth_password,
            )
        )
        print(f"inbound trunk:  {trunk_id}")
        print(f"dispatch rule:  {rule_id}")
        print(f"agent:          {args.agent}  (channel=sip)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
