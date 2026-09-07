"""Application settings: every env var the WhatsApp agent reads, one class.

Rules:
- Construct via get_settings() INSIDE functions, never at import time —
  offline tests import these modules without any env configured.
- Fields default to empty/None and validate late (at the constructor that
  needs them), preserving the app's original fail-late behavior.
- Deliberately NOT here: provider API keys consumed inside the voiceagent
  library / langchain (GOOGLE_API_KEY, DEEPGRAM_API_KEY, ...) and the
  dynamic PRICE_* namespace in pricing.py.
"""

from __future__ import annotations

import json
import logging

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # -- core --------------------------------------------------------------
    database_url: str = ""
    port: int = 8080

    # -- Meta / WhatsApp -----------------------------------------------------
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = "voiceagent"
    whatsapp_app_secret: str = ""     # "" -> webhook signature check off (dev)
    whatsapp_recipient: str = ""      # dev default callee for CLI `call`

    # -- Cal.com / business -----------------------------------------------------
    cal_api_key: str = ""
    cal_event_type_id: int = 0
    cal_timezone: str = "Asia/Kolkata"
    business_name: str = "our office"

    # -- engine defaults (overridable per request / runtime config) -----------
    default_provider: str = "gemini-live"
    default_brain: str = "single"
    voiceagent_language: str = "en-IN"
    voiceagent_voice_id: str | None = None
    scheduler_model: str = "gemini-3.6-flash"
    scheduler_base_url: str | None = None
    scheduler_api_key_env: str = "CUSTOM_LLM_API_KEY"
    split_asr: str = "deepgram"
    split_asr_model: str | None = None
    split_asr_language: str = "multi"
    split_tts: str = "cartesia"
    split_tts_model: str | None = None
    recap_enabled: bool = True
    recap_model: str | None = None
    recap_min_user_turns: int = 2
    judge_model: str = "gemini-3.6-flash"

    # -- service ---------------------------------------------------------------
    redis_url: str | None = None      # None -> Redis features disabled (fail-open)
    metrics_token: str = ""           # "" -> /report /costs answer 404
    admin_token: str = ""             # "" -> falls back to metrics_token
    webhook_rate_per_min: int = 20
    admin_rate_per_min: int = 30
    max_concurrent_calls: int = 3     # capacity gate for CallManager (0 pauses new calls)
    shutdown_grace_s: float = 15.0
    log_json: bool = False


def get_settings() -> Settings:
    """Fresh read every call (cheap; only hit at startup/session setup).
    Uncached on purpose: tests and runtime config mutate the environment."""
    return Settings()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def logging_setup(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    root = logging.getLogger()
    if root.handlers:  # already configured (e.g. uvicorn or a test runner)
        return
    handler = logging.StreamHandler()
    if settings.log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(logging.INFO)
