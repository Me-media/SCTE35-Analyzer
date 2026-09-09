# syntax=docker/dockerfile:1
#
# Single image, two build stages:
#   1. "frontend-build": compiles the React/Vite GUI to static files.
#   2. final stage: Python/FastAPI backend + ffmpeg, serving the API,
#      WebSocket, and the built frontend from ONE process/port.
#
# Build + run:
#   docker compose up -d --build
# See the README for the full git-based install/update workflow.

# ---------------------------------------------------------------------------
FROM node:22-slim AS frontend-build

WORKDIR /frontend
COPY frontend/package.json ./
# No package-lock.json is committed (this repo pins compatible ^-ranges in
# package.json instead) -- `npm install` resolves and installs against
# those ranges. If you commit a lockfile later, switch this to `npm ci`
# for fully reproducible builds.
RUN npm install

COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS final

# ffmpeg: required for IDR JPEG snapshots (--snapshot-dir equivalent),
# on-demand segment->MP4 remux for browser playback, and the HLS/DASH
# ingest relay. Debian/Ubuntu base per the project's own convention.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY --from=frontend-build /frontend/dist ./frontend_dist
COPY CHANGELOG.md ./CHANGELOG.md

ENV SCTE35_ANALYZER_STORAGE_DIR=/data \
    SCTE35_ANALYZER_FRONTEND_DIST=/app/frontend_dist \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
