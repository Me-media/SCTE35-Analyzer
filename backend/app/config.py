"""Runtime configuration for the scte35-analyzer backend.

Everything here is overridable via environment variables so the Docker
image needs no code changes between deployments -- see docker-compose.yml
and the README for the actual defaults shipped in the container.
"""
import os

# Root directory everything this app writes to lives under: the sqlite
# database, per-job uploaded files, IDR snapshots, and raw TS segments.
# Mounted as a named volume in docker-compose.yml so it survives a
# container recreate (`docker compose up -d --build` after a `git pull`).
STORAGE_DIR = os.environ.get("SCTE35_ANALYZER_STORAGE_DIR", "/data")

DB_PATH = os.path.join(STORAGE_DIR, "scte35_analyzer.db")
# v0.7.0 renamed the project (and this filename) from dai_verifier_gui.db.
# An existing deployment's sqlite file is still under the old name -- see
# _migrate_legacy_db_file(), called from ensure_dirs() below, before
# Database(DB_PATH) is ever opened.
_LEGACY_DB_PATH = os.path.join(STORAGE_DIR, "dai_verifier_gui.db")
JOBS_DIR = os.path.join(STORAGE_DIR, "jobs")

# Absolute ceiling on how many raw TS bytes a single ffmpeg-relayed
# HLS/DASH ingest is allowed to buffer in the OS socket queue before the
# kernel starts dropping -- purely informational today, kept here as the
# single place this would be tuned from if it becomes configurable later.
UDP_RECV_BUFFER_BYTES = int(os.environ.get("SCTE35_ANALYZER_UDP_RECV_BUFFER_BYTES", 8 * 1024 * 1024))

# Local loopback port range the ffmpeg HLS/DASH relay picks an ephemeral
# port from (see app/ingest/ffmpeg_relay.py). 0 lets the OS assign one
# freely, which is the default and normally all you need.
FFMPEG_RELAY_FIXED_PORT = os.environ.get("SCTE35_ANALYZER_FFMPEG_RELAY_PORT")

# Where the built frontend's static files live inside the container image
# (see the top-level Dockerfile's frontend build stage). Overridable so
# `uvicorn app.main:app` also works for local backend-only development
# against a separately-run `npm run dev` frontend.
FRONTEND_DIST_DIR = os.environ.get("SCTE35_ANALYZER_FRONTEND_DIST", "/app/frontend_dist")

MAX_UPLOAD_BYTES = int(os.environ.get("SCTE35_ANALYZER_MAX_UPLOAD_BYTES", 20 * 1024 * 1024 * 1024))  # 20 GiB

# CHANGELOG.md, copied into the image alongside app/ and frontend_dist/ (see
# the Dockerfile) so GET /api/changelog -- what backs the "click the version
# number" changelog view in the GUI -- can serve the exact file that shipped
# with this build, not a link out to GitHub that may be ahead of or behind
# what's actually deployed. Missing (e.g. local `uvicorn app.main:app` dev
# without the Docker copy step) is handled as a normal 404, same spirit as
# FRONTEND_DIST_DIR above -- not fatal, just unavailable outside the image.
CHANGELOG_PATH = os.environ.get("SCTE35_ANALYZER_CHANGELOG", "/app/CHANGELOG.md")


def ensure_dirs():
    os.makedirs(STORAGE_DIR, exist_ok=True)
    os.makedirs(JOBS_DIR, exist_ok=True)
    _migrate_legacy_db_file()


def _migrate_legacy_db_file():
    """Renames an existing deployment's sqlite file from the pre-v0.7.0
    name (dai_verifier_gui.db) to the current one (scte35_analyzer.db),
    the FIRST time this runs after the upgrade. Without this, the app
    would just silently open a fresh, empty database under the new name
    and every job/marker/snapshot collected so far would look like it had
    vanished -- the data would still be on disk, just under a filename
    nothing looks for anymore. A no-op once migrated (or on a genuinely
    fresh install, where neither file exists yet). Also moves sqlite's
    WAL-mode sidecar files (-wal/-shm), which can hold committed data not
    yet checkpointed into the main file -- see db.py's `PRAGMA
    journal_mode=WAL`."""
    if os.path.exists(DB_PATH) or not os.path.exists(_LEGACY_DB_PATH):
        return
    for suffix in ("", "-wal", "-shm"):
        src, dst = _LEGACY_DB_PATH + suffix, DB_PATH + suffix
        if os.path.exists(src):
            os.rename(src, dst)


def job_dir(job_id: str) -> str:
    return os.path.join(JOBS_DIR, job_id)


def job_snapshot_dir(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "snapshots")


def job_segment_dir(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "segments")


def job_segment_mp4_dir(job_id: str) -> str:
    return os.path.join(job_dir(job_id), "segments_mp4")


def job_upload_path(job_id: str, filename: str) -> str:
    safe_name = os.path.basename(filename) or "upload.ts"
    return os.path.join(job_dir(job_id), safe_name)
