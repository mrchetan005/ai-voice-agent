"""WhatsApp agent CLI (dev + ops tool). Every command runs the same
FastAPI app the production container runs — commands only differ in which
managers start and whether an outbound call is placed.

    uv run --env-file .env whatsapp-agent serve            # everything (= production)
    uv run --env-file .env whatsapp-agent call             # outbound test call
    uv run --env-file .env whatsapp-agent call --provider split --voice kavita
    uv run --env-file .env whatsapp-agent inbound          # answer loop only
    uv run --env-file .env whatsapp-agent chat             # chat only
    uv run --env-file .env whatsapp-agent audit --days 7 --sample 10

Needs a public HTTPS URL to the webhook port (ngrok in dev; Caddy/your
domain in production) configured in the Meta App dashboard with
WHATSAPP_VERIFY_TOKEN, subscribed to `calls` and `messages`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn
from fastapi import FastAPI

from whatsapp_agent.channels.call_manager import PROVIDERS
from whatsapp_agent.config import get_settings, logging_setup

logger = logging.getLogger("whatsapp_agent")


async def _run_app(app: FastAPI, port: int) -> tuple[uvicorn.Server, asyncio.Task]:
    """Run uvicorn in-process; returns once startup (lifespan) finished."""
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.05)
    if task.done():
        task.result()  # surface the startup failure
        raise RuntimeError("server exited during startup")
    return server, task


async def _cmd_serve(args: argparse.Namespace) -> int:
    from whatsapp_agent.api.app import create_app

    _, task = await _run_app(create_app(), args.port)
    print(f"whatsapp-agent serving on :{args.port} (webhooks + calls + chat); Ctrl+C to stop")
    await task  # uvicorn handles SIGINT/SIGTERM -> graceful lifespan shutdown
    return 0


async def _cmd_inbound(args: argparse.Namespace) -> int:
    from whatsapp_agent.api.app import create_app

    _, task = await _run_app(create_app(start_chat_manager=False), args.port)
    print("inbound mode: waiting for calls — open the business chat on "
          "WhatsApp and tap the call button")
    await task
    return 0


async def _cmd_chat(args: argparse.Namespace) -> int:
    from whatsapp_agent.api.app import create_app

    _, task = await _run_app(create_app(start_call_manager=False), args.port)
    print("chat mode: waiting for WhatsApp messages…")
    await task
    return 0


async def _cmd_call(args: argparse.Namespace) -> int:
    from whatsapp_agent.api.app import create_app
    from whatsapp_agent.channels.call_manager import CallBusy

    app = create_app(start_call_manager=False, start_chat_manager=False)
    server, task = await _run_app(app, args.port)
    try:
        return await app.state.calls.run_outbound(
            peer=args.to or None,
            provider=args.provider,
            brain=args.brain,
            voice=args.voice,
            skip_permission=args.skip_permission,
            permission_only=args.permission_only,
        )
    except CallBusy:
        print("another call is already active")
        return 2
    finally:
        server.should_exit = True
        await task


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="whatsapp-agent",
                                     description="WhatsApp assistant (chat + voice calls)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_port(p: argparse.ArgumentParser) -> None:
        p.add_argument("--port", type=int, default=None,
                       help="webhook port (default: PORT setting, 8080)")

    p_serve = sub.add_parser("serve", help="webhooks + inbound calls + chat, all in one")
    add_port(p_serve)

    p_call = sub.add_parser("call", help="place an outbound call")
    add_port(p_call)
    p_call.add_argument("--to", default=None, help="callee (default: WHATSAPP_RECIPIENT)")
    p_call.add_argument(
        "--provider", choices=PROVIDERS, default="gemini-live",
        help="voice engine: gemini-live (default; supports single brain), "
             "openai-realtime, or split (Deepgram STT + Cartesia TTS; "
             "SPLIT_* settings; both force dual brain)",
    )
    p_call.add_argument(
        "--voice", default=None,
        help="rohan/kavita (Cartesia aliases), a raw Cartesia id, a Gemini "
             "prebuilt name, or an OpenAI voice",
    )
    p_call.add_argument(
        "--brain", choices=("single", "dual"), default="single",
        help="single: Gemini Live calls tools directly (lowest latency; "
             "gemini-live only); dual: LangGraph agent behind the voice "
             "engine (SCHEDULER_MODEL picks its LLM)",
    )
    p_call.add_argument("--skip-permission", action="store_true",
                        help="permission already granted in the last 7 days")
    p_call.add_argument("--permission-only", action="store_true",
                        help="send the permission request and exit")

    p_inbound = sub.add_parser("inbound", help="answer calls to the business number")
    add_port(p_inbound)

    p_chat = sub.add_parser("chat", help="handle WhatsApp TEXT messages")
    add_port(p_chat)

    p_audit = sub.add_parser("audit", help="sampled hallucination audit")
    p_audit.add_argument("--days", type=int, default=7)
    p_audit.add_argument("--sample", type=int, default=10)
    p_audit.add_argument("--model", default=None)
    p_audit.add_argument("--dry-run", action="store_true")

    return parser.parse_args(argv)


def cli() -> None:
    """Console entry point (`whatsapp-agent` after install)."""
    logging_setup()
    if sys.platform == "win32":
        # async psycopg cannot run on the default ProactorEventLoop.
        # The only place this policy is set — Linux deploys don't need it.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    args = parse_args()
    if getattr(args, "port", None) is None and hasattr(args, "port"):
        args.port = get_settings().port
    try:
        if args.command == "serve":
            entry = _cmd_serve(args)
        elif args.command == "call":
            entry = _cmd_call(args)
        elif args.command == "inbound":
            entry = _cmd_inbound(args)
        elif args.command == "chat":
            entry = _cmd_chat(args)
        else:  # audit
            from whatsapp_agent.agent.audit import run_audit

            entry = run_audit(args.days, args.sample, args.model, args.dry_run)
        sys.exit(asyncio.run(entry))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli()
