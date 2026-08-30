# syntax=docker/dockerfile:1
# ---- build stage: resolve deps with uv against the lockfile -----------------
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Deps first (cached layer independent of source changes)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra booking

# Then the project itself. --no-editable: install INTO the venv so the
# runtime stage needs only .venv (editable installs point back at ./src,
# which doesn't exist in the final image).
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --extra booking

# ---- runtime stage: slim python, non-root, venv only -----------------------
FROM python:3.11-slim-bookworm
RUN useradd --create-home app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8080

# Webhook server answers (403 on bad token still proves liveness).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD \
    python -c "import urllib.request,urllib.error,sys;\
exec('try: urllib.request.urlopen(\"http://127.0.0.1:8080/webhook\", timeout=3)\nexcept urllib.error.HTTPError: pass\nexcept Exception: sys.exit(1)')"

# Config via env (.env is NOT baked in — pass with --env-file at run time).
ENTRYPOINT ["appointment-booker"]
CMD ["--inbound"]
