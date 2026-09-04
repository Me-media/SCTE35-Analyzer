"""Job lifecycle: create, start (in its own OS process), stream live
events out (persisted to sqlite + broadcast to WebSocket subscribers), stop.

One job == one app.core.probe.Probe instance driven by one ingest source
(uploaded file / live UDP-or-RTP multicast / HLS or DASH via the ffmpeg
relay). Each job's ingest now runs in its own multiprocessing.Process
rather than a threading.Thread. A thread-per-job model was tried first,
but Python's GIL means only one thread's bytecode executes at a time no
matter how many CPU cores the host has -- with several jobs "running
concurrently" on threads, they were still all fighting over a single
core for the CPU-bound part of this work (the pure-Python per-packet
demux and NAL-unit scanning in app/core/probe.py). A process per job gets
each one genuinely scheduled onto its own core by the OS, so N jobs
running at once can use up to N cores. (This is about N *separate* jobs
running in parallel -- it does not, and can't usefully, split one job's
inherently-sequential packet stream across multiple cores.)

The ffmpeg subprocess calls elsewhere in this app (snapshot JPEG decode,
mp4 remux, the HLS/DASH relay's own ffmpeg process) were never the
bottleneck -- they already run as separate OS processes today and
already get scheduled across cores by the OS regardless of this change.

Because multiprocessing on Linux defaults to fork(), and a sqlite3
connection must not be shared/reused across a fork, each job's child
process opens its own fresh Database(config.DB_PATH) connection rather
than inheriting the parent's -- see _job_worker_main below. That's also
why Database.__init__ now sets PRAGMA busy_timeout (see db.py): multiple
OS processes can now genuinely write concurrently, not just multiple
threads serialized behind one in-process lock.

The parent process never touches the child's Probe or DB connection.
Instead the child puts (kind, data) tuples onto a multiprocessing.Queue
as it emits events -- the same events it just persisted via its own DB
connection -- and a small drainer thread in the parent (one per running
job) reads that queue and forwards each event to the existing
_broadcast() machinery below, completely unchanged. The WebSocket/live-UI
path required no changes at all: it still just sees on_event-shaped
(kind, data) pairs arriving from *a* background thread, same as before.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import threading
from types import SimpleNamespace
from typing import Optional

from app import config
from app.core import probe as core
from app.db import Database
from app.ingest import ffmpeg_relay, file_ingest, udp_ingest
from app.jobs import cleanup

log = logging.getLogger("scte35_analyzer.jobs")

DEFAULT_TUNING = {
    "program": None,
    "pid_video": None,
    "pid_scte35": None,
    "codec": None,
    "tolerance_ms": 6000.0,
    "max_early_ms": 50.0,
    "timeout_s": 12.0,
    "ok_threshold_ms": 41.0,
    "preroll_tolerance_ms": 500.0,
    "min_time_to_event_ms": 4000.0,
    "include_cra": False,
    "snapshot_enabled": True,
    "snapshot_all_idr": False,
    "pre_frames": 0,
    "time_to_event_snapshot": "off",
    "preroll_snapshot": "off",
    "segment_save_enabled": True,
    "segment_window_s": 60.0,
    "segment_no_preroll": False,
    "segment_max_files": 500,
}


def _parse_pid(value):
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    return int(str(value), 0)


def build_probe_args(tuning: dict, snapshot_dir: Optional[str], ts_dump_dir: Optional[str]) -> SimpleNamespace:
    t = {**DEFAULT_TUNING, **(tuning or {})}
    return SimpleNamespace(
        program=t["program"],
        pid_video=_parse_pid(t["pid_video"]),
        pid_scte35=_parse_pid(t["pid_scte35"]),
        codec=t["codec"] or None,
        tolerance_ms=float(t["tolerance_ms"]),
        max_early_ms=float(t["max_early_ms"]),
        timeout_s=float(t["timeout_s"]),
        ok_threshold_ms=float(t["ok_threshold_ms"]),
        preroll_tolerance_ms=float(t["preroll_tolerance_ms"]),
        min_time_to_event_ms=float(t["min_time_to_event_ms"]),
        include_cra=bool(t["include_cra"]),
        verbose=False,
        csv_out=None,
        json_out=None,
        scte35_out=None,
        scte35_log_file=None,
        # The snapshot output directory is needed not just for the plain
        # matched-IDR snapshot (snapshot_enabled) but also for the
        # cue_arrival_frame/target_pts_frame/realtime_deadline_frame
        # reference-snapshot modes below, which write into the same
        # directory independently of that toggle. same_as_matched_idr is
        # the one mode that genuinely still depends on snapshot_enabled --
        # it just relabels the matched-IDR snapshot, so with that toggle off
        # there is nothing for it to relabel.
        snapshot_dir=(snapshot_dir if (
            t["snapshot_enabled"]
            or t["time_to_event_snapshot"] not in (None, "off")
            or t["preroll_snapshot"] not in (None, "off")
        ) else None),
        snapshot_enabled=bool(t["snapshot_enabled"]),
        snapshot_all_idr=bool(t["snapshot_all_idr"]),
        pre_frames=int(t["pre_frames"] or 0),
        time_to_event_snapshot=t["time_to_event_snapshot"] or "off",
        preroll_snapshot=t["preroll_snapshot"] or "off",
        au_buffer_size=None,
        ts_dump_dir=ts_dump_dir if t["segment_save_enabled"] else None,
        ts_dump_window=float(t["segment_window_s"]),
        ts_dump_all=False,
        ts_dump_no_preroll=bool(t["segment_no_preroll"]),
        ts_dump_max_files=(int(t["segment_max_files"]) if t["segment_max_files"] else None),
        # Detailed video info (GOP/pixel format/color space/framerate/
        # interlaced-vs-progressive, see Probe.__init__) -- unlike the
        # other tuning knobs above this isn't exposed as a per-job option
        # in the GUI: it's cheap (one ffprobe call every 20s on a small
        # local sample) and has no real reason to ever be off for a GUI
        # job, unlike e.g. clip saving which trades disk space.
        video_info_enabled=True,
        video_info_interval_s=20.0,
    )


def _make_on_event(db: Database, job_id: str, event_queue: "multiprocessing.Queue"):
    """Builds the on_event(kind, data) callback that a job's Probe calls.
    Runs inside the per-job child process: `db` is that process's own
    sqlite connection (never the parent's -- see _job_worker_main), and
    every event is also put on `event_queue` so the parent's drainer
    thread can forward it to WebSocket subscribers."""
    def on_event(kind, data):
        try:
            if kind == "pid_info":
                db.set_job_pid_info(job_id, data)
            elif kind == "scte35_cue":
                db.insert_cue(job_id, data)
            elif kind == "match_result":
                # Mirror the segment_saved/reference_snapshot_saved
                # pattern below: without this, a LIVE match_result
                # broadcast (as opposed to one replayed from the DB on
                # WebSocket backlog) reaches the browser with id/job_id
                # missing -- JobDetail.tsx keys its marker list on
                # m.id, so every live marker before the next backlog
                # replay collided on the same (undefined) key, and
                # MarkerTable's pre-frame snapshot images used the
                # (also undefined) marker.job_id and 404'd.
                data = dict(data)
                marker_id = db.insert_marker(job_id, data)
                data["id"] = marker_id
                data["job_id"] = job_id
            elif kind == "segment_saved":
                data = dict(data)
                seg_id = db.insert_segment(job_id, data)
                data["id"] = seg_id
            elif kind == "snapshot_saved":
                db.insert_snapshot(job_id, data)
            elif kind == "reference_snapshot_saved":
                data = dict(data)
                cue_snap_id = db.insert_cue_snapshot(job_id, data)
                data["id"] = cue_snap_id
            elif kind == "status":
                db.set_job_status(job_id, data.get("status"), data.get("message"))
            elif kind == "video_info":
                db.set_job_video_info(job_id, data)
        except Exception:  # noqa: BLE001 -- persistence must never take down the ingest process
            log.exception("Failed to persist event kind=%s for job=%s", kind, job_id)
        try:
            event_queue.put((kind, data))
        except Exception:  # noqa: BLE001 -- a broadcast failure must never take down ingest either
            log.exception("Failed to enqueue event kind=%s for job=%s for broadcast", kind, job_id)
    return on_event


def _command_listener(job_id: str, probe: "core.Probe", command_queue: "multiprocessing.Queue"):
    """Runs in a daemon thread inside the per-job child process: the only
    direction of traffic this queue carries is parent -> child (the
    opposite of event_queue above), currently just an on-demand "refresh
    video info now" trigger from the GUI's Refresh button rather than
    waiting for the next periodic sample (see Probe.video_info_interval_s).
    Deliberately not joined anywhere -- it's daemon and carries no state
    that needs flushing, so it just dies with the process on normal exit
    or on stop_job()'s terminate()."""
    while True:
        try:
            cmd = command_queue.get()
        except Exception:  # noqa: BLE001 -- e.g. queue/pipe torn down under us at shutdown
            return
        if cmd is None:
            return
        if cmd == "refresh_video_info":
            try:
                probe._sample_video_info()
            except Exception:  # noqa: BLE001 -- never let a bad command kill ingest
                log.exception("Job %s: on-demand video_info refresh failed", job_id)


def _job_worker_main(job_id, source_type, source, tuning, snapshot_dir, segment_dir,
                      event_queue, stop_event, command_queue=None):
    """Entry point for the per-job child process (spawned via fork() on
    Linux, the deployment target). Must be a plain module-level function --
    multiprocessing has to pickle the target and its args, which rules out
    a bound method or a closure over JobManager/a live Database/Probe
    object. Everything needed is passed in as plain, picklable values
    (job_id/strings/dicts/the multiprocessing Queue and Event) and this
    function builds its own DB connection and Probe locally, fresh in the
    child -- a sqlite3 connection inherited from the parent across a fork
    is explicitly unsupported by sqlite3."""
    db = Database(config.DB_PATH)
    on_event = _make_on_event(db, job_id, event_queue)

    def on_status(status, message):
        on_event("status", {"status": status, "message": message})

    try:
        args = build_probe_args(tuning, snapshot_dir, segment_dir)
        probe = core.Probe(args, on_event=on_event)
        if command_queue is not None:
            threading.Thread(
                target=_command_listener, args=(job_id, probe, command_queue),
                daemon=True, name=f"job-{job_id}-cmd").start()
        if source_type == "file":
            file_ingest.run(probe, source["path"], stop_event, on_status=on_status)
        elif source_type == "udp":
            udp_ingest.run(probe, source["addr"], int(source["port"]),
                            source.get("iface") or None, source.get("transport", "auto"),
                            stop_event, on_status=on_status)
        elif source_type in ("hls", "dash"):
            ffmpeg_relay.run(probe, source["url"], source_type, stop_event, on_status=on_status)
        else:
            on_status("error", f"Unknown source_type {source_type!r}")
    except Exception as exc:  # noqa: BLE001 -- last-resort: never let the child die silently
        log.exception("Job %s worker process crashed", job_id)
        try:
            on_status("error", str(exc))
        except Exception:  # noqa: BLE001
            pass
    finally:
        # Sentinel: tells the parent's drainer thread this job is done so
        # it can stop blocking on event_queue.get() and let itself be
        # joined instead of leaking a thread per finished job.
        try:
            event_queue.put(None)
        except Exception:  # noqa: BLE001
            pass


class JobRuntime:
    def __init__(self, job_id):
        self.job_id = job_id
        self.process: Optional[multiprocessing.Process] = None
        self.stop_event = multiprocessing.Event()
        self.event_queue: Optional[multiprocessing.Queue] = None
        self.command_queue: Optional[multiprocessing.Queue] = None
        self.drain_thread: Optional[threading.Thread] = None


class JobManager:
    def __init__(self, db: Database):
        self.db = db
        self._runtimes: dict[str, JobRuntime] = {}
        self._subscribers: dict[str, set] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Called once from FastAPI's startup hook, on the loop that will
        run the whole app -- broadcasts arrive from a per-job drainer
        thread (see _drain_events) and need this to hand them back to
        that loop."""
        self._loop = loop

    # -- WebSocket subscriber bookkeeping ---------------------------------

    def subscribe(self, job_id, websocket):
        self._subscribers.setdefault(job_id, set()).add(websocket)

    def unsubscribe(self, job_id, websocket):
        subs = self._subscribers.get(job_id)
        if subs:
            subs.discard(websocket)

    async def _async_broadcast(self, job_id, kind, data):
        subs = list(self._subscribers.get(job_id, ()))
        if not subs:
            return
        import json
        message = json.dumps({"kind": kind, "data": data}, default=str)
        for ws in subs:
            try:
                await ws.send_text(message)
            except Exception:  # noqa: BLE001 -- a dead socket is cleaned up on its own receive loop
                pass

    def _broadcast(self, job_id, kind, data):
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._async_broadcast(job_id, kind, data), self._loop)
        except RuntimeError:
            pass  # loop already closed (shutdown race) -- nothing to do

    # -- job creation / lifecycle ------------------------------------------

    def create_job(self, name: str, source_type: str, source_config: dict, tuning_config: dict) -> str:
        job_id = self.db.create_job(name, source_type, source_config, tuning_config)
        os.makedirs(config.job_dir(job_id), exist_ok=True)
        os.makedirs(config.job_snapshot_dir(job_id), exist_ok=True)
        os.makedirs(config.job_segment_dir(job_id), exist_ok=True)
        return job_id

    def _drain_events(self, job_id: str, event_queue: "multiprocessing.Queue"):
        """Runs in a lightweight parent-side thread, one per running job:
        blocks on the child process's event queue and forwards each item
        to the existing broadcast path. This is the only bridge between
        the child process and the parent's WebSocket subscribers, and is
        why nothing downstream of _broadcast() needed to change."""
        while True:
            item = event_queue.get()
            if item is None:  # sentinel put by _job_worker_main's finally block
                return
            kind, data = item
            self._broadcast(job_id, kind, data)

    def start_job(self, job_id: str):
        job = self.db.get_job(job_id)
        if job is None:
            raise KeyError(job_id)

        runtime = JobRuntime(job_id)
        runtime.event_queue = multiprocessing.Queue()
        runtime.command_queue = multiprocessing.Queue()
        self._runtimes[job_id] = runtime

        tuning = job["tuning_config"]
        source = job["source_config"]
        source_type = job["source_type"]

        snapshot_dir = config.job_snapshot_dir(job_id)
        segment_dir = config.job_segment_dir(job_id)

        runtime.process = multiprocessing.Process(
            target=_job_worker_main,
            args=(job_id, source_type, source, tuning, snapshot_dir, segment_dir,
                  runtime.event_queue, runtime.stop_event, runtime.command_queue),
            daemon=True,
            name=f"job-{job_id}",
        )
        runtime.drain_thread = threading.Thread(
            target=self._drain_events, args=(job_id, runtime.event_queue),
            daemon=True, name=f"job-{job_id}-drain",
        )
        runtime.drain_thread.start()
        runtime.process.start()

    def stop_job(self, job_id: str, timeout=10.0):
        runtime = self._runtimes.get(job_id)
        if runtime is None:
            return False
        runtime.stop_event.set()
        if runtime.process:
            runtime.process.join(timeout=timeout)
            if runtime.process.is_alive():
                # Unlike a stuck Python thread (which can never be forced
                # to stop), this is a real OS process, so it can be. This
                # is a genuine improvement over the old thread-based
                # stop_job(), which had no recourse here at all beyond
                # waiting forever.
                log.warning("Job %s did not stop within %.1fs, terminating its process", job_id, timeout)
                runtime.process.terminate()
                runtime.process.join(timeout=5.0)
        if runtime.drain_thread:
            runtime.drain_thread.join(timeout=5.0)
        return True

    def is_running(self, job_id: str) -> bool:
        runtime = self._runtimes.get(job_id)
        return bool(runtime and runtime.process and runtime.process.is_alive())

    def refresh_video_info(self, job_id: str) -> bool:
        """Ask a running job's child process to sample+ffprobe the video
        stream right now instead of waiting for the next periodic sample
        (see Probe.video_info_interval_s / _video_info_worker). Returns
        False (no-op) if the job isn't currently running -- there's no
        live buffer to sample from otherwise. Best-effort like everything
        else on this queue: if the child is mid-shutdown and never picks
        the command up, nothing breaks, the request is just dropped."""
        runtime = self._runtimes.get(job_id)
        if not (runtime and runtime.process and runtime.process.is_alive() and runtime.command_queue):
            return False
        try:
            runtime.command_queue.put("refresh_video_info")
        except Exception:  # noqa: BLE001 -- e.g. queue torn down in a shutdown race
            return False
        return True

    def reconcile_orphaned_jobs(self):
        """Called once at startup: JobManager's in-memory _runtimes dict is
        empty on a fresh process (a container restart, a deploy), so any
        job the DB still shows as 'starting'/'running' from a previous
        process life had its actual ingest process die with that process
        without ever getting the chance to record a final status. Mark
        those as 'stopped' now instead of leaving a job stuck showing
        'running' forever with no way to stop it (stop_job() only knows
        about runtimes created in *this* process)."""
        for job in self.db.list_jobs():
            if job["status"] in ("starting", "running"):
                self.db.set_job_status(
                    job["id"], "stopped",
                    "Backend restarted while this job was active; it was not resumed.")

    # -- retention / cleanup ------------------------------------------------
    #
    # Deliberately NOT part of the per-job child process: a stopped/
    # finished job's old segments/snapshots still need to age out even
    # though its ingest process no longer exists, so this has to live
    # here in the parent, running for every job regardless of whether it's
    # currently active. See app/jobs/cleanup.py for the actual purge logic
    # -- this class only owns the periodic scheduling.

    def cleanup_job_now(self, job_id: str, *, segment_max_age_s=None, snapshot_max_age_s=None) -> Optional[dict]:
        """On-demand purge for one job (POST /api/jobs/{id}/cleanup), at a
        caller-chosen age independent of that job's saved retention
        settings -- e.g. a one-off "clear everything older than a week"
        without committing to that as the ongoing policy. Returns None if
        the job doesn't exist, otherwise the purge_job() summary dict."""
        if self.db.get_job(job_id) is None:
            return None
        return cleanup.purge_job(
            self.db, job_id, segment_max_age_s=segment_max_age_s, snapshot_max_age_s=snapshot_max_age_s)

    def start_retention_sweep(self, interval_s: float = 900.0):
        """Called once from FastAPI's startup hook (see main.py's
        lifespan), alongside bind_event_loop/reconcile_orphaned_jobs.
        Stores the task on self so it isn't garbage-collected mid-flight
        (a bare fire-and-forget asyncio.create_task() is only weakly
        referenced) and so stop_retention_sweep() can cancel it cleanly on
        shutdown instead of leaving it to die uncleanly with the process."""
        self._retention_task = asyncio.get_running_loop().create_task(self._retention_sweep_loop(interval_s))

    async def stop_retention_sweep(self):
        task = getattr(self, "_retention_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _retention_sweep_loop(self, interval_s: float):
        """Runs for the lifetime of the app. Sleeps first (there's nothing
        to sweep in the seconds right after startup, and it avoids every
        restart immediately hammering the DB+filesystem), then purges on
        a fixed interval. Each job's own segment_retention_s/
        snapshot_retention_s (None = keep forever, the default -- opt-in
        only) decides what happens to it; see app/jobs/cleanup.py."""
        while True:
            try:
                await asyncio.sleep(interval_s)
                totals = await asyncio.get_running_loop().run_in_executor(None, cleanup.purge_all_jobs, self.db)
                if totals["jobs_swept"]:
                    log.info(
                        "Retention sweep: %d job(s), %d segment(s) and %d snapshot file(s) deleted, %.1f MB freed",
                        totals["jobs_swept"], totals["segments_deleted"],
                        totals["snapshot_files_deleted"], totals["bytes_freed"] / 1e6)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- one bad sweep must not kill the loop forever
                log.exception("Retention sweep iteration failed")
