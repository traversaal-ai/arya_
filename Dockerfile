# syntax=docker/dockerfile:1
# One image for both the `api` and `agent-worker` services.
# Stage 1 builds the frontend; stage 2 installs python deps and prebakes
# model files; the final stage is a slim runtime.

# ARGs used in FROM lines must be declared before the first FROM.
# 3.12 (not newer): docling/mem0ai dependency trees lag the newest python.
ARG PYTHON_VERSION=3.12

# --- Frontend build ---
FROM node:22-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend ./
RUN npm run build

# --- Python base ---
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS base
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV HF_HOME=/app/.cache/huggingface
ENV TORCH_HOME=/app/.cache/torch
# docling's layout model calls torch.compile, whose JIT needs a C++
# compiler at runtime — absent in the slim image (seen live:
# "InvalidCxxCompiler: no working C++ compiler"). Eager mode is fine for
# background ingestion and beats shipping a ~250 MB toolchain.
ENV TORCH_COMPILE_DISABLE=1
ENV TORCHDYNAMO_DISABLE=1

# --- Backend build ---
FROM base AS build
RUN apt-get update && apt-get install -y gcc g++ python3-dev \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN mkdir -p src
# BuildKit cache mount: wheels persist across builds, so even a dependency
# change only downloads what actually changed. copy link-mode because the
# venv lives outside the cache mount (hardlinks can't cross that boundary).
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_LINK_MODE=copy uv sync --locked
# Pre-download plugin model files (Silero VAD, turn detector) so the first
# call doesn't pay the download cost.
RUN uv run --module livekit.agents download-files
# Same for docling's PDF models (layout + table structure): they land in
# HF_HOME, which ships inside the image — otherwise the first PDF after
# every rebuild stalls for minutes re-downloading them (seen live).
# EasyOCR models are NOT prebaked: they only download when a scanned page
# actually needs OCR.
RUN uv run python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('docling-project/docling-models'); \
    snapshot_download('docling-project/docling-layout-heron')"
COPY backend ./

# --- Runtime ---
FROM base
# docling's PDF pipeline imports OpenCV, which needs these native libs —
# the slim base ships none of them, so every PDF ingestion died with
# "ImportError: libxcb.so.1" (DOCX takes a different path and worked).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libxcb1 \
    libpango-1.0-0 libpangoft2-1.0-0 fontconfig fonts-inter \
  && rm -rf /var/lib/apt/lists/*
# libpango*/fontconfig serve WeasyPrint (server-side itinerary PDFs);
# fonts-inter matches the builder's body font so PDF typography mirrors
# the page (Space Grotesk falls back to Inter in the PDF).
# uid 1000 matches the default WSL/Linux host user, so the bind-mounted
# backend/data directory stays writable from both sides.
ARG UID=1000
RUN adduser --disabled-password --gecos "" --home "/app" \
    --shell "/sbin/nologin" --uid "${UID}" appuser
COPY --from=build --chown=appuser:appuser /app /app
COPY --from=frontend --chown=appuser:appuser /fe/dist /app/frontend/dist
# Operator docs, served in-app at #/docs/deployment (see _DOCS in api.py).
# Last content layer on purpose: editing a doc must not invalidate the
# dependency layers above it.
COPY --chown=appuser:appuser docs /app/docs
WORKDIR /app/backend
USER appuser
CMD ["uv", "run", "python", "src/agent.py", "start"]
