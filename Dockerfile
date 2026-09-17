# syntax=docker/dockerfile:1
#
# Multi-stage build: resolve the locked dependency set with uv, then copy the
# ready-made virtualenv into a slim runtime image with a non-root user.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first (cached unless the lock changes); README.md is read by the
# build backend, so it must be present for the project install below.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra rest --extra mcp --no-install-project

COPY fusion/ ./fusion/
RUN uv sync --frozen --no-dev --extra rest --extra mcp


FROM python:3.13-slim

WORKDIR /app

RUN groupadd -r fusion && useradd -r -g fusion fusion \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app/data /app/logs \
    && chown -R fusion:fusion /app

COPY --from=builder --chown=fusion:fusion /app/.venv /app/.venv
COPY --from=builder --chown=fusion:fusion /app/fusion /app/fusion

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER fusion

# Shell form on purpose: the port variable must expand here.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f "http://localhost:${FUSION_PORT:-9000}/health" || exit 1

EXPOSE 9000

# Exec form runs no shell, so nothing here may rely on ${VAR} expansion: the
# Warp URL, port and every other setting come from the environment
# (WARP_URL, FUSION_PORT, ...) through Settings.from_env().
CMD ["fusion-rest", "--auto-discover"]
