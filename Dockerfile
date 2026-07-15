# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder: resolve and install dependencies with uv, then the project itself.
# Two-layer install so the dependency layer stays cached when only src changes.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

# Pinned uv binary (matches the version used to produce uv.lock locally).
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 1) Dependency layer (cacheable): only lockfiles, NOT the project source.
#    --no-install-project is required: hatchling would otherwise try to build
#    src/second_brain, which is not present yet on this layer.
#    On Linux the lock resolves torch to the CPU wheel (no CUDA/nvidia/triton);
#    faster-whisper (with ctranslate2) is cross-platform and installs everywhere.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 2) Project layer: bring in the source and install the package (console
#    scripts land on /app/.venv/bin).
COPY src ./src
COPY README.md ./README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime: slim image with only what the processes need at run time.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# ffmpeg is mandatory: the voice worker calls ensure_runtime() on startup and
# refuses to start without it. libgomp1 is the OpenMP runtime CTranslate2 (under
# faster-whisper) links against; without it the first transcription crashes.
# ca-certs are needed for HTTPS to OpenRouter and HuggingFace.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ffmpeg ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (compose also pins user: "1001:1001").
RUN groupadd --gid 1001 app \
    && useradd --uid 1001 --gid 1001 --create-home --home-dir /home/app app

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/app/data/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/app/data/hf-cache

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# Import-smoke in the runtime stage (venv present, libgomp1 installed): a broken
# ctranslate2 wheel or a missing OpenMP runtime fails the BUILD, not production.
RUN python -c "import faster_whisper, ctranslate2"

# HF model weights are NOT baked; they live in the mounted ./data volume.
RUN mkdir -p /app/data && chown -R 1001:1001 /app/data

USER 1001:1001

# No hard CMD: compose selects the process via `command:` per service
# (second-brain-local-polling / second-brain-local-voice-worker).
