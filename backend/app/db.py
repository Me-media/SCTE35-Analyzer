"""Thin, dependency-free persistence layer (stdlib sqlite3) for jobs,
SCTE-35 cues, match/miss markers, IDR snapshots, and saved raw-TS segments.

Kept deliberately simple (no ORM) -- this tool's write volume is bounded by
how often a real feed emits SCTE-35 messages (a handful a minute even on a
busy channel), nowhere near where sqlite's single-writer model would be a
problem. One connection is shared across threads (ingest threads call in
via Probe's on_event callback, API/WebSocket handlers call in from the
FastAPI event loop's threadpool) behind a single lock, with WAL mode on so
concurrent readers are never blocked by a writer.
"""
import json
import os
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_config TEXT NOT NULL,
    tuning_config TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'starting',
    error TEXT,
    pid_info TEXT,
    video_info TEXT,
    segment_retention_s REAL,
    snapshot_retention_s REAL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS cues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    cue_seq INTEGER,
    wallclock TEXT,
    command_type TEXT,
    descriptor_summary TEXT,
    section_hex TEXT,
    cue TEXT
);
CREATE INDEX IF NOT EXISTS idx_cues_job ON cues(job_id, id);

CREATE TABLE IF NOT EXISTS markers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    cue_seq INTEGER,
    event_id TEXT,
    command_type TEXT,
    out_of_network INTEGER,
    target_pts_s REAL,
    raw_pts_time_s REAL,
    pts_adjustment_ticks INTEGER,
    idr_pts_s REAL,
    delta_ms REAL,
    verdict TEXT,
    codec TEXT,
    au_kind TEXT,
    segmentation_summary TEXT,
    snapshot_path TEXT,
    pre_frame_snapshot_paths TEXT,
    time_to_event_ms REAL,
    actual_preroll_ms REAL,
    preroll_delta_ms REAL,
    preroll_verdict TEXT,
    signal_verdict TEXT,
    gop_verdict TEXT,
    near_miss_ms REAL,
    wallclock TEXT
);
CREATE INDEX IF NOT EXISTS idx_markers_job ON markers(job_id, id);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    path TEXT,
    mp4_path TEXT,
    window_start_wallclock TEXT,
    window_end_wallclock TEXT,
    window_s REAL,
    included_previous_window INTEGER,
    event_count INTEGER,
    event_cue_seqs TEXT,
    size_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_segments_job ON segments(job_id, id);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    path TEXT,
    kind TEXT,
    idr_ticks INTEGER,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_job ON snapshots(job_id, id);

-- Reference-frame snapshots: the "time to event" / "pre-roll" captures
-- (see Probe.time_to_event_snapshot / preroll_snapshot). Kept separate
-- from `snapshots` above (which is the per-IDR/pre-frame JPEG record)
-- since these are keyed by cue_seq + a mode tag, not by IDR PTS, and a job
-- may have zero, one, or several per cue depending on which modes are on.
CREATE TABLE IF NOT EXISTS cue_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    cue_seq INTEGER,
    tag TEXT,
    path TEXT,
    frame_pts_s REAL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cue_snapshots_job ON cue_snapshots(job_id, id);
"""


def _now():
    """Naive (no offset suffix) ISO8601, always UTC -- explicitly
    time.gmtime(), not time.localtime()/a bare strftime(), so this stays
    UTC regardless of the container's configured timezone. The GUI labels
    every field built from this "UTC" and separately renders a local-time
    conversion for the viewer (see frontend/src/lib/time.ts); that label
    would be a lie if this ever silently reverted to time.localtime()."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


class Database:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # WAL allows concurrent readers alongside one writer, but two
        # separate processes both writing (see jobs/manager.py -- each
        # job's ingest now runs in its own OS process, each opening its own
        # connection to this same file, for multi-core use) can still
        # collide on the single write lock. Without a busy_timeout, sqlite3
        # raises "database is locked" immediately instead of waiting;
        # this job's write volume is low (a handful of events a minute)
        # so a short wait-and-retry window is enough to never see that in
        # practice.
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._migrate()

    def _migrate(self):
        """CREATE TABLE IF NOT EXISTS (see SCHEMA above) only creates a
        table the first time -- it does nothing to an already-existing
        table from a previous version, so a column added to `jobs` after
        an existing deployment's first run (like `video_info` here) needs
        an explicit ALTER TABLE or every upgrade would crash the first
        time that column is written/read ("no such column"). No real
        migration framework for a single, still-evolving table -- just add
        the ALTER here, guarded by a check against the live schema, every
        time a new column is introduced."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "video_info" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN video_info TEXT")
            self._conn.commit()
        if "segment_retention_s" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN segment_retention_s REAL")
            self._conn.commit()
        if "snapshot_retention_s" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN snapshot_retention_s REAL")
            self._conn.commit()
        marker_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(markers)").fetchall()}
        if "gop_verdict" not in marker_cols:
            self._conn.execute("ALTER TABLE markers ADD COLUMN gop_verdict TEXT")
            self._conn.commit()
        if "near_miss_ms" not in marker_cols:
            self._conn.execute("ALTER TABLE markers ADD COLUMN near_miss_ms REAL")
            self._conn.commit()

    # -- jobs -------------------------------------------------------------

    def create_job(self, name, source_type, source_config: dict, tuning_config: dict) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, name, source_type, source_config, tuning_config, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, 'starting', ?)",
                (job_id, name, source_type, json.dumps(source_config),
                 json.dumps(tuning_config), _now()),
            )
            self._conn.commit()
        return job_id

    def set_job_status(self, job_id, status, error=None):
        with self._lock:
            if status == "running":
                # Also clear any stale `error` from a previous failed run --
                # without this, restarting a job that previously errored
                # (see jobs/manager.py's restart_job()) would keep showing
                # that old error message in the UI even while the job is
                # now running successfully again.
                self._conn.execute(
                    "UPDATE jobs SET status=?, started_at=?, error=NULL WHERE id=?",
                    (status, _now(), job_id))
            elif status in ("finished", "stopped", "error"):
                self._conn.execute(
                    "UPDATE jobs SET status=?, error=?, ended_at=? WHERE id=?",
                    (status, error, _now(), job_id))
            else:
                self._conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
            self._conn.commit()

    def set_job_source_config(self, job_id, source_config: dict):
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET source_config=? WHERE id=?",
                (json.dumps(source_config), job_id))
            self._conn.commit()

    def set_job_tuning_config(self, job_id, tuning_config: dict):
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET tuning_config=? WHERE id=?",
                (json.dumps(tuning_config), job_id))
            self._conn.commit()

    def set_job_name(self, job_id, name: str):
        with self._lock:
            self._conn.execute("UPDATE jobs SET name=? WHERE id=?", (name, job_id))
            self._conn.commit()

    def set_job_pid_info(self, job_id, pid_info: dict):
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET pid_info=? WHERE id=?", (json.dumps(pid_info), job_id))
            self._conn.commit()

    def set_job_video_info(self, job_id, video_info: dict):
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET video_info=? WHERE id=?", (json.dumps(video_info), job_id))
            self._conn.commit()

    def set_job_retention(self, job_id, segment_retention_s, snapshot_retention_s):
        """Either argument is None for "keep forever" (the default,
        unchanged from before this feature existed -- an existing job
        never starts auto-deleting anything just because it was upgraded).
        Deliberately NOT gated on the job being stopped like
        set_job_tuning_config/set_job_source_config are: retention is
        enforced entirely from the parent process (see jobs/cleanup.py and
        JobManager's periodic sweep), never touches the running child's
        Probe, so there's no reason to make you stop a live job just to
        change how long its old data is kept."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET segment_retention_s=?, snapshot_retention_s=? WHERE id=?",
                (segment_retention_s, snapshot_retention_s, job_id))
            self._conn.commit()

    def get_job(self, job_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [_row_to_job(r) for r in rows]

    def delete_job(self, job_id):
        with self._lock:
            for table in ("cues", "markers", "segments", "snapshots", "cue_snapshots"):
                self._conn.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
            self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self._conn.commit()

    def flush_job_data(self, job_id):
        """Like delete_job(), minus the last line -- clears every data
        table for this job (cues, markers, segments, snapshots,
        cue_snapshots) but leaves the `jobs` row itself alone, so the
        job's name/source_config/tuning_config/retention settings survive.
        Used by app.jobs.cleanup.flush_all_job_data() ("Flush all data" in
        the GUI). Returns the deleted segments/snapshots/cue_snapshots
        rows (the caller still needs their `path`/`mp4_path` to remove the
        on-disk files -- this method never touches the filesystem itself,
        same split of responsibility as delete_segments_older_than() etc.)
        plus how many marker/cue rows were removed, via each DELETE's own
        cursor.rowcount rather than a separate SELECT COUNT(*)."""
        with self._lock:
            seg_rows = [_row_to_segment(r) for r in self._conn.execute(
                "SELECT * FROM segments WHERE job_id=?", (job_id,)).fetchall()]
            snap_rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM snapshots WHERE job_id=?", (job_id,)).fetchall()]
            cue_snap_rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM cue_snapshots WHERE job_id=?", (job_id,)).fetchall()]
            marker_count = self._conn.execute(
                "DELETE FROM markers WHERE job_id=?", (job_id,)).rowcount
            cue_count = self._conn.execute(
                "DELETE FROM cues WHERE job_id=?", (job_id,)).rowcount
            self._conn.execute("DELETE FROM segments WHERE job_id=?", (job_id,))
            self._conn.execute("DELETE FROM snapshots WHERE job_id=?", (job_id,))
            self._conn.execute("DELETE FROM cue_snapshots WHERE job_id=?", (job_id,))
            self._conn.commit()
        return {
            "markers_deleted": marker_count,
            "cues_deleted": cue_count,
            "segments": seg_rows,
            "snapshots": snap_rows,
            "cue_snapshots": cue_snap_rows,
        }

    # -- cues ---------------------------------------------------------------

    def insert_cue(self, job_id, record: dict):
        with self._lock:
            self._conn.execute(
                "INSERT INTO cues (job_id, cue_seq, wallclock, command_type, "
                "descriptor_summary, section_hex, cue) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, record.get("cue_seq"), record.get("wallclock"),
                 record.get("command_type"), json.dumps(record.get("descriptor_summary")),
                 record.get("section_hex"), json.dumps(record.get("cue"), default=str)),
            )
            self._conn.commit()

    def list_cues(self, job_id, since_id=0, limit=500):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cues WHERE job_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (job_id, since_id, limit)).fetchall()
        return [_row_to_cue(r) for r in rows]

    # -- markers --------------------------------------------------------

    def insert_marker(self, job_id, record: dict):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO markers (job_id, cue_seq, event_id, command_type, out_of_network, "
                "target_pts_s, raw_pts_time_s, pts_adjustment_ticks, idr_pts_s, delta_ms, verdict, "
                "codec, au_kind, segmentation_summary, snapshot_path, pre_frame_snapshot_paths, "
                "time_to_event_ms, actual_preroll_ms, preroll_delta_ms, preroll_verdict, "
                "signal_verdict, gop_verdict, near_miss_ms, wallclock) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, record.get("cue_seq"),
                 None if record.get("event_id") is None else str(record.get("event_id")),
                 record.get("command_type"),
                 None if record.get("out_of_network") is None else int(bool(record.get("out_of_network"))),
                 record.get("target_pts_s"), record.get("raw_pts_time_s"),
                 record.get("pts_adjustment_ticks"),
                 record.get("idr_pts_s"), record.get("delta_ms"),
                 record.get("verdict"), record.get("codec"), record.get("au_kind"),
                 json.dumps(record.get("segmentation_summary")), record.get("snapshot_path"),
                 json.dumps(record.get("pre_frame_snapshot_paths")),
                 record.get("time_to_event_ms"), record.get("actual_preroll_ms"),
                 record.get("preroll_delta_ms"), record.get("preroll_verdict"),
                 record.get("signal_verdict"), record.get("gop_verdict"),
                 record.get("near_miss_ms"), record.get("wallclock")),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_markers(self, job_id, since_id=0, limit=2000):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM markers WHERE job_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (job_id, since_id, limit)).fetchall()
        return [_row_to_marker(r) for r in rows]

    # -- segments ("clips") -----------------------------------------------

    def insert_segment(self, job_id, meta: dict):
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO segments (job_id, path, window_start_wallclock, "
                "window_end_wallclock, window_s, included_previous_window, event_count, "
                "event_cue_seqs, size_bytes) VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, meta.get("path"), meta.get("window_start_wallclock"),
                 meta.get("window_end_wallclock"), meta.get("window_s"),
                 int(bool(meta.get("included_previous_window"))), meta.get("event_count"),
                 json.dumps(meta.get("event_cue_seqs")), meta.get("size_bytes")),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_segments(self, job_id, since_id=0, limit=500):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM segments WHERE job_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (job_id, since_id, limit)).fetchall()
        return [_row_to_segment(r) for r in rows]

    def get_segment(self, job_id, segment_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM segments WHERE job_id=? AND id=?", (job_id, segment_id)).fetchone()
        return _row_to_segment(row) if row else None

    def set_segment_mp4_path(self, segment_id, mp4_path):
        with self._lock:
            self._conn.execute(
                "UPDATE segments SET mp4_path=? WHERE id=?", (mp4_path, segment_id))
            self._conn.commit()

    def delete_segments_older_than(self, job_id, cutoff_iso: str):
        """Deletes every segment row for this job whose window ended before
        cutoff_iso (an ISO8601 wallclock string, see app.jobs.cleanup) and
        returns the deleted rows -- the caller (app.jobs.cleanup.purge_job)
        still needs `path`/`mp4_path` from each to remove the actual files
        on disk, which this method deliberately does not touch itself (no
        filesystem access from the DB layer). A segment with no
        window_end_wallclock (shouldn't normally happen) is never matched,
        so it can't be swept up by an off/garbled timestamp."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM segments WHERE job_id=? AND window_end_wallclock IS NOT NULL "
                "AND window_end_wallclock < ?", (job_id, cutoff_iso)).fetchall()
            rows = [_row_to_segment(r) for r in rows]
            self._conn.execute(
                "DELETE FROM segments WHERE job_id=? AND window_end_wallclock IS NOT NULL "
                "AND window_end_wallclock < ?", (job_id, cutoff_iso))
            self._conn.commit()
        return rows

    # -- snapshots ----------------------------------------------------------

    def insert_snapshot(self, job_id, data: dict):
        with self._lock:
            self._conn.execute(
                "INSERT INTO snapshots (job_id, path, kind, idr_ticks, created_at) "
                "VALUES (?,?,?,?,?)",
                (job_id, data.get("path"), data.get("kind"), data.get("idr_ticks"), _now()),
            )
            self._conn.commit()

    def list_snapshots(self, job_id, since_id=0, limit=1000):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM snapshots WHERE job_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (job_id, since_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def delete_snapshots_older_than(self, job_id, cutoff_iso: str):
        """See delete_segments_older_than -- same shape, keyed on
        `created_at` (set at insert time) instead of a window end."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM snapshots WHERE job_id=? AND created_at < ?",
                (job_id, cutoff_iso)).fetchall()
            rows = [dict(r) for r in rows]
            self._conn.execute(
                "DELETE FROM snapshots WHERE job_id=? AND created_at < ?", (job_id, cutoff_iso))
            self._conn.commit()
        return rows

    # -- cue (reference-frame) snapshots -------------------------------------

    def insert_cue_snapshot(self, job_id, data: dict):
        frame_pts = data.get("frame_pts")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO cue_snapshots (job_id, cue_seq, tag, path, frame_pts_s, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (job_id, data.get("cue_seq"), data.get("tag"), data.get("path"),
                 None if frame_pts is None else frame_pts / 90000.0, _now()),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_cue_snapshots(self, job_id, since_id=0, limit=1000):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cue_snapshots WHERE job_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (job_id, since_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def delete_cue_snapshots_older_than(self, job_id, cutoff_iso: str):
        """See delete_segments_older_than -- same shape, keyed on
        `created_at` (set at insert time)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cue_snapshots WHERE job_id=? AND created_at < ?",
                (job_id, cutoff_iso)).fetchall()
            rows = [dict(r) for r in rows]
            self._conn.execute(
                "DELETE FROM cue_snapshots WHERE job_id=? AND created_at < ?", (job_id, cutoff_iso))
            self._conn.commit()
        return rows


def _row_to_job(row):
    d = dict(row)
    d["source_config"] = json.loads(d["source_config"]) if d.get("source_config") else {}
    d["tuning_config"] = json.loads(d["tuning_config"]) if d.get("tuning_config") else {}
    d["pid_info"] = json.loads(d["pid_info"]) if d.get("pid_info") else None
    d["video_info"] = json.loads(d["video_info"]) if d.get("video_info") else None
    return d


def _row_to_cue(row):
    d = dict(row)
    d["descriptor_summary"] = json.loads(d["descriptor_summary"]) if d.get("descriptor_summary") else []
    d["cue"] = json.loads(d["cue"]) if d.get("cue") else None
    return d


def _row_to_marker(row):
    d = dict(row)
    d["segmentation_summary"] = json.loads(d["segmentation_summary"]) if d.get("segmentation_summary") else []
    d["pre_frame_snapshot_paths"] = (json.loads(d["pre_frame_snapshot_paths"])
                                      if d.get("pre_frame_snapshot_paths") else [])
    d["out_of_network"] = None if d.get("out_of_network") is None else bool(d["out_of_network"])
    return d


def _row_to_segment(row):
    d = dict(row)
    d["event_cue_seqs"] = json.loads(d["event_cue_seqs"]) if d.get("event_cue_seqs") else []
    d["included_previous_window"] = bool(d.get("included_previous_window"))
    return d
