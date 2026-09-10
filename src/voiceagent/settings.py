"""Platform configuration.

Every settings class reads from the environment (and an optional .env file).
Secrets are env-only by design — never hard-coded, never in YAML.
`load_settings()` validates everything at startup and reports *all* problems
at once instead of failing one variable at a time.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = ".env"


class LiveKitSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIVEKIT_", env_file=_ENV_FILE, extra="ignore")

    url: str = "ws://localhost:7880"
    # URL browsers should connect to, when it differs from `url` (e.g. inside
    # docker-compose the API reaches LiveKit at ws://livekit:7880 while the
    # browser needs ws://localhost:7880). Empty = same as `url`.
    public_url: str = ""
    api_key: str = ""
    api_secret: str = ""

    @property
    def effective_public_url(self) -> str:
        return self.public_url or self.url

    def require_credentials(self) -> list[str]:
        """Names of missing required variables (empty when valid)."""
        missing = []
        if not self.api_key:
            missing.append("LIVEKIT_API_KEY")
        if not self.api_secret:
            missing.append("LIVEKIT_API_SECRET")
        return missing


class PlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_", env_file=_ENV_FILE, extra="ignore")

    api_token: str = ""
    port: int = 8080
    database_url: str = ""
    redis_url: str = ""
    sip_trunk_id: str = ""  # default outbound SIP trunk for /v1/calls/sip
    demo_dir: str = "examples/browser-demo"  # served at /demo when it exists


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORKER_", env_file=_ENV_FILE, extra="ignore")

    prometheus_port: int = 9100
    idle_processes: int = 2
    load_threshold: float = 0.7
    drain_timeout_s: int = 600


class AgentSourceSettings(BaseSettings):
    """Where the worker and the platform API discover agent definitions."""

    model_config = SettingsConfigDict(env_prefix="VOICEAGENT_", env_file=_ENV_FILE, extra="ignore")

    agents: str = ""  # comma-separated "pkg.module:attr" paths to VoiceAgent objects
    agents_file: str = ""  # path to a YAML file of agent configs
    # LiveKit agent_name the worker fleet registers under. One worker serves
    # ALL loaded agents: dispatch metadata (SessionMetadata.agent_id) selects
    # which VoiceAgent runs. Run separate fleets by using different names.
    worker_name: str = "voiceagent"


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore")

    otel_exporter_otlp_endpoint: str = ""
    metrics_enabled: bool = True


class RecordingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RECORDING_", env_file=_ENV_FILE, extra="ignore")

    enabled: bool = False
    s3_endpoint: str = ""
    bucket: str = ""
    access_key: str = ""
    secret_key: str = ""
    region: str = ""
    prefix: str = "recordings"

    def require_storage(self) -> list[str]:
        if not self.enabled:
            return []
        missing = []
        for field, env in (
            ("bucket", "RECORDING_BUCKET"),
            ("access_key", "RECORDING_ACCESS_KEY"),
            ("secret_key", "RECORDING_SECRET_KEY"),
        ):
            if not getattr(self, field):
                missing.append(env)
        return missing


@dataclass(slots=True)
class Settings:
    livekit: LiveKitSettings
    platform: PlatformSettings
    worker: WorkerSettings
    agent_source: AgentSourceSettings
    observability: ObservabilitySettings
    recording: RecordingSettings


class SettingsError(RuntimeError):
    """Raised at startup when required configuration is missing or invalid."""


def load_settings(*, require_livekit: bool = False) -> Settings:
    """Build all settings groups; aggregate every problem into one error."""
    problems: list[str] = []
    groups: dict[str, BaseSettings] = {}
    for name, cls in (
        ("livekit", LiveKitSettings),
        ("platform", PlatformSettings),
        ("worker", WorkerSettings),
        ("agent_source", AgentSourceSettings),
        ("observability", ObservabilitySettings),
        ("recording", RecordingSettings),
    ):
        try:
            groups[name] = cls()
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                problems.append(f"{cls.__name__}.{loc}: {err['msg']}")

    if not problems:
        if require_livekit:
            problems += groups["livekit"].require_credentials()  # type: ignore[attr-defined]
        problems += groups["recording"].require_storage()  # type: ignore[attr-defined]

    if problems:
        detail = "\n  - ".join(problems)
        raise SettingsError(f"invalid configuration:\n  - {detail}")

    return Settings(**groups)  # type: ignore[arg-type]
