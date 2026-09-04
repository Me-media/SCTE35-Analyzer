"""Age-based cleanup of a job's saved segments (TS dumps + their cached MP4
remux) and snapshots (matched-IDR/pre-frame JPEGs, plus the separate
time-to-event/pre-roll reference-frame snapshots) -- deliberately entirely
separate from app/core/probe.py's own count-based `--ts-dump-max-files`
pruning (which runs inside the per-job child process, only while that job
is actually running, and only for segments). This module is used from TWO
places that both need to agree on exactly what "older than X" means and
how a row + its file(s) get removed:

  1. JobManager's periodic sweep (see manager.py's _cleanup_sweep_loop),
     which runs in the parent process for ALL jobs regardless of whether
     they're currently running -- a stopped/finished job's ingest process
     no longer exists to prune anything itself, so its old data would
     never age out without this.
  2. The on-demand "clean up now" endpoint (POST /api/jobs/{id}/cleanup),
     for an immediate one-off purge at a caller-chosen age instead of
     waiting for the next sweep or relying on the job's saved retention.

Retention is a parent-process/housekeeping concern: it never touches a
running Probe, so (unlike tuning_config edits) it's safe to change or
trigger while a job is live -- see db.py's set_job_retention().
"""
import logging
import os
import time

log = logging.getLogger("scte35_analyzer.cleanup")

# Preset choices shown in the GUI, in seconds. Kept here (not just in the
# frontend) so the backend can validate an explicit override rather than
# accepting an arbitrary caller-supplied number of seconds unchecked.
RETENTION_PRESETS_S = {
    "1h": 3600,
    "6h": 6 * 3600,
    "12h": 12 * 3600,
    "1d": 24 * 3600,
    "1w": 7 * 24 * 3600,
}


def cutoff_iso(max_age_s: float) -> str:
    """ISO8601 wallclock cutoff, in the exact format app.db._now() writes
    (%Y-%m-%dT%H:%M:%S) -- segments/snapshots are matched against this as
    plain text, not parsed back into datetimes, since that format sorts
    correctly lexicographically. max_age_s=0 means "older than right now",
    i.e. everything already saved (a same-second race is the only way
    something saved in the current second survives) -- the one-off "delete
    everything in this category" case, no separate code path needed."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - max_age_s))


def _unlink(path) -> int:
    """Best-effort delete; returns the freed byte count (0 if the path is
    empty/already gone -- a file missing on disk is not an error here,
    its DB row still gets removed)."""
    if not path:
        return 0
    try:
        size = os.path.getsize(path)
        os.remove(path)
        return size
    except OSError:
        return 0


def purge_job(db, job_id: str, *, segment_max_age_s=None, snapshot_max_age_s=None) -> dict:
    """Deletes segments and/or snapshots (both plain IDR snapshots and
    reference-frame cue_snapshots) older than the given threshold, for ONE
    job. Either threshold can be None to skip that category entirely (a
    job with no retention configured for it, or a "just snapshots"
    one-off cleanup). Returns a summary for the caller to show the user;
    never raises for an individual missing file -- see _unlink."""
    result = {"segments_deleted": 0, "snapshot_files_deleted": 0, "bytes_freed": 0}

    if segment_max_age_s is not None:
        rows = db.delete_segments_older_than(job_id, cutoff_iso(segment_max_age_s))
        for row in rows:
            result["bytes_freed"] += _unlink(row.get("path"))
            result["bytes_freed"] += _unlink(row.get("mp4_path"))
        result["segments_deleted"] = len(rows)
        if rows:
            log.info("Job %s: purged %d segment(s) older than %.0fs", job_id, len(rows), segment_max_age_s)

    if snapshot_max_age_s is not None:
        cutoff = cutoff_iso(snapshot_max_age_s)
        rows = db.delete_snapshots_older_than(job_id, cutoff)
        rows += db.delete_cue_snapshots_older_than(job_id, cutoff)
        for row in rows:
            result["bytes_freed"] += _unlink(row.get("path"))
        result["snapshot_files_deleted"] = len(rows)
        if rows:
            log.info("Job %s: purged %d snapshot file(s) older than %.0fs", job_id, len(rows), snapshot_max_age_s)

    return result


def purge_all_jobs(db) -> dict:
    """Runs purge_job() for every job using ITS OWN saved retention
    settings (jobs with both left at "keep forever" / None are skipped
    entirely, which is every job by default -- this is opt-in). This is
    what the periodic sweep calls; kept as a plain function (not a
    JobManager method) so it can be unit-tested without spinning up a
    JobManager/event loop at all."""
    totals = {"jobs_swept": 0, "segments_deleted": 0, "snapshot_files_deleted": 0, "bytes_freed": 0}
    for job in db.list_jobs():
        seg_age = job.get("segment_retention_s")
        snap_age = job.get("snapshot_retention_s")
        if not seg_age and not snap_age:
            continue
        try:
            result = purge_job(db, job["id"], segment_max_age_s=seg_age, snapshot_max_age_s=snap_age)
        except Exception:  # noqa: BLE001 -- one job's cleanup failing must not skip the rest
            log.exception("Retention sweep failed for job %s", job["id"])
            continue
        totals["jobs_swept"] += 1
        totals["segments_deleted"] += result["segments_deleted"]
        totals["snapshot_files_deleted"] += result["snapshot_files_deleted"]
        totals["bytes_freed"] += result["bytes_freed"]
    return totals
