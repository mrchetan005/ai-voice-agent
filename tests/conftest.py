"""Shared pytest setup. Offline unit tests run by default; live tests
(real APIs/DB) are opt-in:  uv run --env-file .env pytest -m live"""

import asyncio
import sys

if sys.platform == "win32":
    # psycopg async cannot run on the default ProactorEventLoop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Hermetic settings: pydantic-settings would otherwise read the developer's
# .env from the CWD, so "token unset" scenarios would depend on what happens
# to be in that file. Real env VARIABLES still apply — live runs use
# `uv run --env-file .env pytest -m live`, which exports them properly.
from whatsapp_agent.config import Settings

Settings.model_config["env_file"] = None
