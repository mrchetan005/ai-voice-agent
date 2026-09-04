"""Shared pytest setup. Offline unit tests run by default; live tests
(real APIs/DB) are opt-in:  uv run --env-file .env pytest -m live"""

import asyncio
import sys

if sys.platform == "win32":
    # psycopg async cannot run on the default ProactorEventLoop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
