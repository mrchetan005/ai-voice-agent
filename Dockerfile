# voiceagent image: platform API, worker, or whatsapp bridge (pick via command).
# Build from repo root:  docker build -t voiceagent .
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    HF_HUB_CACHE=/opt/models \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prom

WORKDIR /app

# Dependency layer (cached until pyproject/uv.lock change).
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra agents --extra platform --extra obs

# Model layer: silero ships in its wheel; the turn-detector models download
# here so code changes never re-download them.
RUN mkdir -p /opt/models /tmp/prom && python -m livekit.agents download-files

# Application layer.
COPY src ./src
COPY examples ./examples
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra agents --extra platform --extra obs

RUN useradd --create-home appuser && chown -R appuser:appuser /app /tmp/prom /opt/models
USER appuser

# voiceagent-api | voiceagent-worker start | voiceagent-whatsapp
CMD ["voiceagent-api"]
