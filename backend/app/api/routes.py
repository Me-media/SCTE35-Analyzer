import csv
import io
import json
import logging
import os
import shutil

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app import config
from app.api.deps import get_db, get_jobs
from app.api.schemas import (
    CleanupRequest, CreateLiveJobRequest, TuningConfig, UpdateJobRequest, UpdateRetentionRequest,
)
from app.db import Database
from app.jobs.manager import JobManager
from app.media import segments as segment_media
from app.version import __version__

log = logging.getLogger("scte35_analyzer.api")
router = APIRouter(prefix="/api")


@router.get("/version")
def get_version():
    """Lets the frontend show which build is actually running -- the
    simplest way for someone to tell a deploy picked up new code instead
    of the browser (or an unreloaded container) still serving the old
    one. See CHANGELOG.md at the repo root for what changed per version."""
    return {"version": __version__}


@router.get("/changelog")
def get_changelog():
    """Backs the "click the version number" changelog view in the GUI --
    serves the exact CHANGELOG.md that shipped with THIS build (copied into
    the image alongside app/ and frontend_dist/, see the Dockerfile and
    config.CHANGELOG_PATH), rather than linking out to GitHub which may be
    ahead of or behind what's actually deployed. 404s (rather than 500ing)
    when the file isn't there -- e.g. local `uvicorn app.main:app` dev
    without the Docker copy step -- same spirit as the FRONTEND_DIST_DIR
    static-mount guard right below this router's routes."""
    if not os.path.isfile(config.CHANGELOG_PATH):
        raise HTTPException(
            status_code=404,
            detail="CHANGELOG.md isn't available in this deployment (expected at "
                   f"{config.CHANGELOG_PATH}).",
        )
    with open(config.CHANGELOG_PATH, "r", encoding="utf-8") as f:
        return {"content": f.read()}

MARKER_CSV_FIELDS = [
    "wallclock", "cue_seq", "event_id", "command_type", "out_of_network",
    "target_pts_s", "raw_pts_time_s", "pts_adjustment_ticks", "idr_pts_s",
    "delta_ms", "verdict", "codec", "au_kind", "segmentation_summary",
    "snapshot_path", "pre_frame_snapshot_paths", "time_to_event_ms",
    "actual_preroll_ms", "preroll_delta_ms", "preroll_verdict", "signal_verdict",
    "gop_verdict", "near_miss_ms",
]


# -- Jobs ----------------------------------------------------------------

@router.post("/jobs/upload")
async def create_upload_job(
    file: UploadFile = File(...),
    name: str = Form(...),
    tuning: str = Form("{}"),
    db: Database = Depends(get_db),
    jobs: JobManager = Depends(get_jobs),
):
    try:
        tuning_dict = TuningConfig(**json.loads(tuning)).model_dump()
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid tuning JSON: {exc}")

    job_id = jobs.create_job(name, "file", {"filename": file.filename}, tuning_dict)
    dest_path = config.job_upload_path(job_id, file.filename or "upload.ts")

    def _write():
        size = 0
        with open(dest_path, "wb") as out:
            while True:
                chunk = file.file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "File exceeds the configured upload size limit")
                out.write(chunk)
        return size

    try:
        size = await run_in_threadpool(_write)
    except Exception:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        db.set_job_status(job_id, "error", "Upload failed or exceeded the size limit")
        raise
    log.info("Job %s: uploaded %s (%d bytes)", job_id, file.filename, size)

    # store the real path now that we know it (create_job only knew the filename)
    db.set_job_source_config(job_id, {"filename": file.filename, "path": dest_path, "size_bytes": size})

    jobs.start_job(job_id)
    return {"job_id": job_id}


@router.post("/jobs/live")
def create_live_job(body: CreateLiveJobRequest, jobs: JobManager = Depends(get_jobs)):
    source = body.source.model_dump()
    job_id = jobs.create_job(body.name, source["type"], source, body.tuning.model_dump())
    jobs.start_job(job_id)
    return {"job_id": job_id}


@router.get("/jobs")
def list_jobs(db: Database = Depends(get_db), jobs: JobManager = Depends(get_jobs)):
    out = []
    for job in db.list_jobs():
        job = dict(job)
        job["is_running"] = jobs.is_running(job["id"])
        out.append(job)
    return out


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Database = Depends(get_db), jobs: JobManager = Depends(get_jobs)):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    job = dict(job)
    job["is_running"] = jobs.is_running(job_id)
    return job


@router.post("/jobs/{job_id}/stop")
def stop_job(job_id: str, jobs: JobManager = Depends(get_jobs)):
    if not jobs.stop_job(job_id):
        raise HTTPException(404, "Job not running (or never started) in this process")
    return {"ok": True}


@router.post("/jobs/{job_id}/restart")
def restart_job(job_id: str, db: Database = Depends(get_db), jobs: JobManager = Depends(get_jobs)):
    """Re-runs a stopped/finished/errored job against its existing,
    unchanged source_config: re-attaches to the same live udp/hls/dash
    source, or reprocesses the same uploaded file from the start. Markers,
    cues, snapshots and clips already collected are kept -- the new run's
    events are simply appended under the same job_id, they are never
    cleared (see start_job()/the job's on_event persistence path, neither
    of which touch existing rows). A job that is currently running can't
    be restarted -- stop it first."""
    if db.get_job(job_id) is None:
        raise HTTPException(404, "Job not found")
    if jobs.is_running(job_id):
        raise HTTPException(409, "Job is already running")
    jobs.start_job(job_id)
    return {"ok": True}


@router.post("/jobs/{job_id}/refresh_video_info")
def refresh_video_info(job_id: str, db: Database = Depends(get_db), jobs: JobManager = Depends(get_jobs)):
    """Asks a running job to re-sample+ffprobe the video stream right now
    instead of waiting for the next periodic sample (every 20s by
    default). No-op (409) if the job isn't currently running -- there's no
    live packet buffer to sample from otherwise; the last video_info
    result stays visible either way."""
    if db.get_job(job_id) is None:
        raise HTTPException(404, "Job not found")
    if not jobs.refresh_video_info(job_id):
        raise HTTPException(409, "Job is not running")
    return {"ok": True}


@router.patch("/jobs/{job_id}")
def update_job(job_id: str, body: UpdateJobRequest,
                db: Database = Depends(get_db), jobs: JobManager = Depends(get_jobs)):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if jobs.is_running(job_id):
        raise HTTPException(409, "Stop the job before editing it")

    if body.source is not None:
        if job["source_type"] == "file":
            raise HTTPException(400, "A file job's source file can't be changed -- create a new job instead")
        if body.source.type != job["source_type"]:
            raise HTTPException(
                400, f"Cannot change a {job['source_type']!r} job's source type to {body.source.type!r}")
        db.set_job_source_config(job_id, body.source.model_dump())

    if body.tuning is not None:
        db.set_job_tuning_config(job_id, body.tuning.model_dump())

    if body.name is not None:
        db.set_job_name(job_id, body.name)

    job = dict(db.get_job(job_id))
    job["is_running"] = jobs.is_running(job_id)
    return job


@router.patch("/jobs/{job_id}/retention")
def update_retention(job_id: str, body: UpdateRetentionRequest, db: Database = Depends(get_db)):
    """How long this job's saved segments (TS dumps)/snapshots are kept --
    enforced by a periodic background sweep, see JobManager's
    _retention_sweep_loop. Unlike PATCH /jobs/{id} above, this is NOT
    gated on the job being stopped: retention never touches a running
    Probe, so there's no reason to require stopping a live channel just
    to change how long its old data is kept."""
    if db.get_job(job_id) is None:
        raise HTTPException(404, "Job not found")
    db.set_job_retention(job_id, body.segment_retention_s, body.snapshot_retention_s)
    return dict(db.get_job(job_id))


@router.post("/jobs/{job_id}/cleanup")
def cleanup_job(job_id: str, body: CleanupRequest,
                 db: Database = Depends(get_db), jobs: JobManager = Depends(get_jobs)):
    """Immediate, one-off purge -- independent of the job's saved
    retention settings (though passing the same values just applies them
    right now instead of waiting for the next sweep). Works regardless of
    whether the job is currently running, same reasoning as the retention
    endpoint above."""
    result = jobs.cleanup_job_now(
        job_id, segment_max_age_s=body.segment_max_age_s, snapshot_max_age_s=body.snapshot_max_age_s)
    if result is None:
        raise HTTPException(404, "Job not found")
    return result


@router.post("/jobs/{job_id}/flush")
def flush_job(job_id: str, jobs: JobManager = Depends(get_jobs)):
    """Deletes ALL saved SCTE-35 data for this job -- every marker, cue,
    segment and snapshot -- while keeping the job itself (name/source/
    tuning/retention settings) intact. "Flush all data" in the GUI's
    Clean up menu: a full reset for re-baselining a channel without
    losing its configuration and having to recreate it. Distinct from
    DELETE /jobs/{id} below, which removes the job entirely. No request
    body -- unlike /cleanup above, this always clears everything, there's
    no partial/age-based variant of a deliberate full wipe."""
    result = jobs.flush_job_data(job_id)
    if result is None:
        raise HTTPException(404, "Job not found")
    return result


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str, db: Database = Depends(get_db), jobs: JobManager = Depends(get_jobs)):
    jobs.stop_job(job_id, timeout=15.0)
    db.delete_job(job_id)
    job_dir = config.job_dir(job_id)
    if os.path.isdir(job_dir):
        shutil.rmtree(job_dir, ignore_errors=True)
    return {"ok": True}


# -- Markers / cues / segments --------------------------------------------

@router.get("/jobs/{job_id}/markers")
def list_markers(job_id: str, since_id: int = 0, limit: int = 2000, db: Database = Depends(get_db)):
    return db.list_markers(job_id, since_id=since_id, limit=limit)


@router.get("/jobs/{job_id}/cues")
def list_cues(job_id: str, since_id: int = 0, limit: int = 500, db: Database = Depends(get_db)):
    return db.list_cues(job_id, since_id=since_id, limit=limit)


@router.get("/jobs/{job_id}/segments")
def list_segments(job_id: str, since_id: int = 0, limit: int = 500, db: Database = Depends(get_db)):
    return db.list_segments(job_id, since_id=since_id, limit=limit)


@router.get("/jobs/{job_id}/cue_snapshots")
def list_cue_snapshots(job_id: str, since_id: int = 0, limit: int = 1000, db: Database = Depends(get_db)):
    return db.list_cue_snapshots(job_id, since_id=since_id, limit=limit)


@router.get("/jobs/{job_id}/markers.csv")
def markers_csv(job_id: str, db: Database = Depends(get_db)):
    markers = db.list_markers(job_id, since_id=0, limit=1_000_000)

    def _generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(MARKER_CSV_FIELDS)
        yield buf.getvalue()
        for m in markers:
            buf = io.StringIO()
            writer = csv.writer(buf)
            row = []
            for field in MARKER_CSV_FIELDS:
                val = m.get(field)
                if field in ("segmentation_summary",):
                    val = "; ".join(val or [])
                elif field == "pre_frame_snapshot_paths":
                    val = "; ".join(val or [])
                row.append("" if val is None else val)
            writer.writerow(row)
            yield buf.getvalue()

    return StreamingResponse(
        _generate(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{job_id}_markers.csv"'})


# -- Media: snapshots + segment playback -----------------------------------

@router.get("/media/snapshots/{job_id}/{filename}")
def get_snapshot(job_id: str, filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(config.job_snapshot_dir(job_id), safe_name)
    if not os.path.isfile(path):
        raise HTTPException(404, "Snapshot not found")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/media/segments/{job_id}/{segment_id}.mp4")
async def get_segment_mp4(job_id: str, segment_id: int, db: Database = Depends(get_db)):
    seg = db.get_segment(job_id, segment_id)
    if seg is None:
        raise HTTPException(404, "Segment not found")
    ts_path = seg["path"]
    if not ts_path or not os.path.isfile(ts_path):
        raise HTTPException(404, "Segment .ts file missing on disk")
    mp4_path = os.path.join(config.job_segment_mp4_dir(job_id), f"{segment_id}.mp4")

    ok, error = await run_in_threadpool(segment_media.ensure_mp4, ts_path, mp4_path)
    if not ok:
        raise HTTPException(500, f"Failed to remux this segment for playback: {error}")
    if not seg.get("mp4_path"):
        db.set_segment_mp4_path(segment_id, mp4_path)
    return FileResponse(mp4_path, media_type="video/mp4")


@router.get("/media/segments/{job_id}/{segment_id}.ts")
def get_segment_ts(job_id: str, segment_id: int, db: Database = Depends(get_db)):
    seg = db.get_segment(job_id, segment_id)
    if seg is None or not seg.get("path") or not os.path.isfile(seg["path"]):
        raise HTTPException(404, "Segment not found")
    return FileResponse(seg["path"], media_type="video/mp2t",
                         filename=os.path.basename(seg["path"]))
