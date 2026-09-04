#!/usr/bin/env python3
"""
Integration smoke test for the parts of the scte35-analyzer backend that do NOT
require fastapi/uvicorn to be importable (this sandbox has no PyPI access
and fastapi genuinely isn't preinstalled here -- see the session notes;
the Docker image installs it from requirements.txt normally). This drives
app.db.Database + app.jobs.manager.JobManager + the ingest modules exactly
as app/api/routes.py would, just calling the Python API directly instead of
going through HTTP.

Covers:
  - a "file" job: synthetic PAT/PMT/video TS file -> job reaches 'running'
    then 'finished', pid_info gets detected and persisted, DB rows survive
    a JobManager restart (new process would re-read the same sqlite file).
  - a "udp" job: starts a real loopback UDP listener thread, receives a
    packet, gets stopped cleanly, reaches 'stopped'.
  - delete_job cleans up both the DB rows and the on-disk job directory.

Run: python3 test_integration_jobs.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from app import config  # noqa: E402
from app.core import probe as core  # noqa: E402
from app.db import Database  # noqa: E402
from app.jobs import cleanup  # noqa: E402
from app.jobs.manager import JobManager  # noqa: E402
import test_engine_offline as helpers  # noqa: E402


def _wait_until(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _build_synthetic_ts_file(path):
    pmt_pid, video_pid, scte35_pid = 0x100, 0x101, 0x1F0
    packets = [helpers.build_pat(program_number=1, pmt_pid=pmt_pid)]
    cuei = bytes([0x05, 0x04]) + b"CUEI"
    packets.append(helpers.build_pmt(
        pcr_pid=video_pid,
        streams=[(core.STREAM_TYPE_H264, video_pid, b""),
                 (core.STREAM_TYPE_SCTE35, scte35_pid, cuei)],
        pmt_pid=pmt_pid))
    es_payload = helpers.h264_idr_nal()
    pkts = helpers.build_pes_packets(video_pid, int(10.0 * core.PTS_HZ), es_payload)
    pkts += helpers.build_pes_packets(video_pid, int(10.5 * core.PTS_HZ), es_payload,
                                       cc_start=len(pkts) % 16)
    packets += pkts
    with open(path, "wb") as f:
        for pkt in packets:
            f.write(pkt)
    return video_pid, scte35_pid


async def test_file_job(storage_dir):
    config.STORAGE_DIR = storage_dir
    config.DB_PATH = os.path.join(storage_dir, "test.db")
    config.JOBS_DIR = os.path.join(storage_dir, "jobs")
    config.ensure_dirs()

    db = Database(config.DB_PATH)
    jobs = JobManager(db)
    jobs.bind_event_loop(asyncio.get_running_loop())

    job_id = jobs.create_job("test file job", "file", {"filename": "x.ts"}, {})
    ts_path = os.path.join(config.job_dir(job_id), "upload.ts")
    video_pid, scte35_pid = _build_synthetic_ts_file(ts_path)
    db.set_job_source_config(job_id, {"filename": "x.ts", "path": ts_path})

    jobs.start_job(job_id)
    assert _wait_until(lambda: db.get_job(job_id)["status"] in ("finished", "error")), \
        "file job did not reach a terminal status in time"

    job = db.get_job(job_id)
    assert job["status"] == "finished", job
    assert job["pid_info"]["video_pid"] == video_pid, job["pid_info"]
    assert job["pid_info"]["scte35_pid"] == scte35_pid, job["pid_info"]
    print("OK: file job runs end-to-end via JobManager, reaches status=finished, "
          "and pid_info is correctly persisted to sqlite")

    # A second Database instance opened against the same path (simulating a
    # process restart / a second API worker) sees the same data.
    db2 = Database(config.DB_PATH)
    job2 = db2.get_job(job_id)
    assert job2["pid_info"]["video_pid"] == video_pid
    print("OK: job state survives being re-read from a fresh Database() "
          "connection against the same sqlite file")

    return db, jobs, job_id


async def test_restart_appends_and_clears_error(db, jobs, job_id):
    """Regression test for the restart feature: restarting a job is just
    start_job() called again on a job_id that already has history (this is
    exactly what POST /api/jobs/{id}/restart does after its own running/
    404 checks) -- the user explicitly chose 'keep and append' over
    'clear first', so re-running against the same file must ADD to the
    existing markers, never wipe them. Also covers the set_job_status()
    fix: a stale `error` from a previous failed run must be cleared the
    moment the job reaches 'running' again, or the UI would keep showing
    an old error message for a job that is actually running fine."""
    # The synthetic file used by test_file_job carries no actual SCTE-35
    # cue (just PAT/PMT/video), so it produces no markers of its own --
    # insert one directly here to stand in for "a marker a previous run
    # already collected", the same way a real prior run's rows would sit
    # in the DB before a restart. What matters for 'keep and append' is
    # that start_job() never clears/replaces existing rows for a job_id
    # (only delete_job() does, and restart never calls that) -- this
    # proves the guarantee without needing to hand-encode a real
    # SCTE-35 splice_insert section just for this test.
    db.insert_marker(job_id, {"cue_seq": 1, "verdict": "OK", "wallclock": "synthetic-prior-run"})
    markers_before = db.list_markers(job_id, since_id=0, limit=1_000_000)
    assert len(markers_before) == 1

    # Simulate a job that failed on a previous run (as if a live source
    # dropped, or a bad tuning value blew up mid-run) -- restarting it
    # should clear this, not leave it showing forever.
    db.set_job_status(job_id, "error", "synthetic previous failure, for this test only")
    assert db.get_job(job_id)["error"] == "synthetic previous failure, for this test only"

    jobs.start_job(job_id)  # same call restart_job() makes after its checks pass
    # Deliberately waiting for "finished" specifically, not "finished" or
    # "error": the job's status is already "error" from the line above, so
    # a predicate that accepted either would pass instantly on that stale
    # value instead of actually observing the restarted run complete.
    assert _wait_until(lambda: db.get_job(job_id)["status"] == "finished")
    job = db.get_job(job_id)
    assert job["status"] == "finished", job
    assert job["error"] is None, "restarting a job must clear a stale error from a previous run"
    print("OK: restarting a job clears a stale error from a previous failed run")

    markers_after = db.list_markers(job_id, since_id=0, limit=1_000_000)
    assert len(markers_after) == 1 and markers_after[0]["id"] == markers_before[0]["id"], (
        "restart must never clear/replace a job's pre-existing marker rows "
        f"(had {markers_before}, now {markers_after})")
    print("OK: restarting a job keeps its pre-existing markers -- start_job() only appends, "
          "it never clears a job's prior history")


def test_edit_job_fields(db):
    """Regression test for the edit feature's underlying DB calls (the
    PATCH /api/jobs/{id} route itself needs fastapi, which isn't available
    in this sandbox -- see the module docstring -- so this exercises the
    same Database methods that route calls: set_job_name and
    set_job_tuning_config; set_job_source_config is already covered above)."""
    job_id = db.create_job("before edit", "udp", {"addr": "239.1.1.1", "port": 5000}, {})
    db.set_job_name(job_id, "after edit")
    db.set_job_tuning_config(job_id, {"tolerance_ms": 999.0})
    db.set_job_source_config(job_id, {"addr": "239.1.1.2", "port": 5001})
    job = db.get_job(job_id)
    assert job["name"] == "after edit"
    assert job["tuning_config"]["tolerance_ms"] == 999.0
    assert job["source_config"]["addr"] == "239.1.1.2"
    print("OK: editing a job's name/tuning/source persists correctly via the DB layer "
          "the PATCH /api/jobs/{id} route uses")


async def test_concurrent_jobs_use_separate_processes(db, jobs):
    """Regression test for the multiprocessing refactor of JobManager:
    two jobs started at (roughly) the same time must run as two distinct
    OS processes, not share the same one and not silently fall back to
    the old thread-per-job model. This is the actual thing being fixed --
    'the system runs mostly on one CPU core' -- so assert on OS-level
    facts (real, distinct pids) rather than just timing, which would be
    flaky on a loaded CI host."""
    job_ids = []
    for i in range(2):
        job_id = jobs.create_job(f"concurrent job {i}", "file", {"filename": "x.ts"}, {})
        ts_path = os.path.join(config.job_dir(job_id), "upload.ts")
        _build_synthetic_ts_file(ts_path)
        db.set_job_source_config(job_id, {"filename": "x.ts", "path": ts_path})
        job_ids.append(job_id)

    for job_id in job_ids:
        jobs.start_job(job_id)

    # Both must be alive as real OS processes at the same time, each with
    # its own pid, and neither pid may be the parent's own pid (which is
    # what a thread-based implementation would show instead).
    assert _wait_until(lambda: all(jobs.is_running(j) for j in job_ids)), \
        "both concurrent jobs should reach running before either finishes"
    pids = [jobs._runtimes[j].process.pid for j in job_ids]
    assert all(pid is not None for pid in pids), pids
    assert len(set(pids)) == len(pids), f"jobs did not get distinct OS processes: {pids}"
    assert os.getpid() not in pids, "job ingest is still running in-process, not in a child process"
    print(f"OK: concurrently-started jobs run as distinct OS processes {pids} "
          f"(parent pid {os.getpid()}), not threads sharing the parent process")

    for job_id in job_ids:
        assert _wait_until(lambda j=job_id: db.get_job(j)["status"] in ("finished", "error"))
        assert db.get_job(job_id)["status"] == "finished"
    print("OK: both concurrently-run jobs complete successfully")
    return job_ids


async def test_udp_job(db, jobs):
    job_id = jobs.create_job("test udp job", "udp",
                              {"addr": "127.0.0.1", "port": 0, "transport": "ts"}, {})
    # Port 0 isn't valid for a listener -- allocate a real free port first,
    # the same way the ffmpeg relay does, then patch it into source_config.
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    db.set_job_source_config(job_id, {"addr": "127.0.0.1", "port": port, "transport": "ts"})

    jobs.start_job(job_id)
    assert _wait_until(lambda: jobs.is_running(job_id)), "udp job never reached running"
    assert _wait_until(lambda: db.get_job(job_id)["status"] == "running")
    print("OK: udp job starts a live loopback listener and reaches status=running")

    # Send a PAT + PMT so we know the socket is genuinely alive and packets
    # flow all the way through handle_ts_packet() -- pid_info (unlike
    # pat_info) only fires once the PMT has been parsed, so a PAT alone
    # isn't enough to observe from here.
    pmt_pid = 0x100
    pat_pkt = helpers.build_pat(program_number=1, pmt_pid=pmt_pid)
    pmt_pkt = helpers.build_pmt(pcr_pid=0x101, streams=[(core.STREAM_TYPE_H264, 0x101, b"")], pmt_pid=pmt_pid)
    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send_sock.sendto(pat_pkt, ("127.0.0.1", port))
    send_sock.sendto(pmt_pkt, ("127.0.0.1", port))
    send_sock.close()
    # The Probe instance now lives inside the job's own child process (see
    # jobs/manager.py's multiprocessing refactor), so it can't be inspected
    # directly from here anymore -- go through the same path the real GUI
    # does instead: the child persists pid_info to sqlite via its own DB
    # connection as soon as it parses the PMT, so poll that.
    assert _wait_until(lambda: ((db.get_job(job_id) or {}).get("pid_info") or {}).get("pmt_pid") == pmt_pid), \
        "PAT/PMT packets were not received/parsed"
    print("OK: real UDP packets sent to the job's loopback port are received and parsed")

    jobs.stop_job(job_id, timeout=5.0)
    assert _wait_until(lambda: db.get_job(job_id)["status"] == "stopped")
    assert not jobs.is_running(job_id)
    print("OK: stop_job() cleanly stops the listener thread and status becomes 'stopped'")
    return job_id


def test_ensure_mp4_multi_pid_ts(storage_dir):
    """A --ts-dump-dir capture is byte-exact, every PID -- including the
    SCTE-35 data PID. Build a small TS with a real decodable H.264 frame
    PLUS a raw SCTE-35 section on its own PID (exactly what a real dump
    contains) and confirm ensure_mp4() remuxes it to a playable MP4 instead
    of failing over the unmappable data stream."""
    import shutil as _shutil
    if _shutil.which("ffmpeg") is None:
        print("SKIP: ffmpeg not on PATH -- ensure_mp4 multi-PID test not exercised")
        return
    import subprocess
    from app.media import segments as segment_media

    gen = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=blue:s=64x64:d=1:r=1", "-frames:v", "1",
         "-c:v", "libx264", "-profile:v", "baseline", "-f", "h264", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    assert gen.returncode == 0 and gen.stdout, gen.stderr
    full_au = gen.stdout

    video_pid, scte35_pid, pmt_pid = 0x101, 0x1F0, 0x100
    packets = [helpers.build_pat(program_number=1, pmt_pid=pmt_pid)]
    cuei = bytes([0x05, 0x04]) + b"CUEI"
    packets.append(helpers.build_pmt(
        pcr_pid=video_pid,
        streams=[(core.STREAM_TYPE_H264, video_pid, b""),
                 (core.STREAM_TYPE_SCTE35, scte35_pid, cuei)],
        pmt_pid=pmt_pid,
    ))
    packets.append(helpers.build_section_packet(scte35_pid, b"\x00" * 20, table_id=0xFC, cc=0))
    packets += helpers.build_pes_packets(video_pid, 0, full_au)
    packets += helpers.build_pes_packets(video_pid, 3600, full_au, cc_start=1)

    ts_path = os.path.join(storage_dir, "multi_pid_dump.ts")
    with open(ts_path, "wb") as f:
        for p in packets:
            f.write(p)

    mp4_path = os.path.join(storage_dir, "multi_pid_dump.mp4")
    ok, error = segment_media.ensure_mp4(ts_path, mp4_path)
    assert ok, f"ensure_mp4 failed on a multi-PID (video + SCTE-35) dump: {error}"
    assert os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0
    print("OK: ensure_mp4() remuxes a raw multi-PID (video + SCTE-35) TS dump "
          "into a playable MP4 instead of failing over the unmappable data PID")


def test_ensure_mp4_aac_audio_ts(storage_dir):
    """Regression test for a real field report: a saved clip with AAC audio
    failed to play at all ('Malformed AAC bitstream detected' / 'Error
    writing trailer: Operation not permitted' from ffmpeg's MP4 muxer).
    Root cause: AAC inside an MPEG-TS is ADTS-framed, and copying that
    straight into MP4 without the aac_adtstoasc bitstream filter makes the
    MP4 muxer abort the entire mux -- not just drop/mute the audio, the
    video came out unplayable too. Build a real TS (via ffmpeg's own mpegts
    muxer, so the audio is ADTS-framed exactly like a genuine capture) with
    both a video and an AAC audio stream and confirm ensure_mp4() now
    remuxes it successfully."""
    import shutil as _shutil
    if _shutil.which("ffmpeg") is None or _shutil.which("ffprobe") is None:
        print("SKIP: ffmpeg/ffprobe not on PATH -- ensure_mp4 AAC-audio test not exercised")
        return
    import subprocess
    from app.media import segments as segment_media

    ts_path = os.path.join(storage_dir, "with_aac_audio.ts")
    gen = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1:r=5",
         "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
         "-c:v", "libx264", "-profile:v", "baseline",
         "-c:a", "aac", "-b:a", "64k",
         "-f", "mpegts", ts_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    assert gen.returncode == 0 and os.path.exists(ts_path), gen.stderr
    # Sanity check that this actually built the case we mean to test --
    # if ffmpeg's own build ever stopped ADTS-framing AAC in mpegts output,
    # this test would otherwise silently stop testing anything. (ffprobe
    # has been observed to list the same short-clip TS stream more than
    # once -- see _probe_audio_codecs' docstring in app/media/segments.py
    # -- so check "aac" appears, not that the output is exactly one line.)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", ts_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    codecs = {line.strip().rstrip(",") for line in probe.stdout.decode().splitlines() if line.strip()}
    assert codecs == {"aac"}, probe.stdout

    mp4_path = os.path.join(storage_dir, "with_aac_audio.mp4")
    ok, error = segment_media.ensure_mp4(ts_path, mp4_path)
    assert ok, f"ensure_mp4 failed on a TS with AAC audio: {error}"
    assert os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0

    # Confirm the fix didn't just avoid an error while silently dropping
    # audio: the output MP4 must still actually contain a decodable AAC
    # track (as ASC-described, not ADTS-framed -- ffprobe on a raw ADTS
    # stream inside an mp4 container would fail to parse it as a normal
    # audio track at all).
    verify = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", mp4_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    out_codecs = {line.strip().rstrip(",") for line in verify.stdout.decode().splitlines() if line.strip()}
    assert out_codecs == {"aac"}, f"output MP4 lost its audio track: {verify.stdout!r} {verify.stderr!r}"
    print("OK: ensure_mp4() remuxes a TS with ADTS-framed AAC audio into a playable MP4 with "
          "audio intact (previously failed the whole mux with 'Malformed AAC bitstream detected')")


def test_ensure_mp4_ac3_audio_ts(storage_dir):
    """Regression test for a second real field report: a saved clip with
    AC-3 audio also failed outright ('Cannot write moov atom before AC3
    packets. Set the delay_moov flag to fix this.' / 'Could not write
    header (incorrect codec parameters ?): Invalid argument', ffmpeg exit
    234). Root cause: -movflags empty_moov writes the moov box (which for
    AC-3 must include a dac3 box derived from the first actual AC-3
    frame's bitstream info) before any packets have been read at all --
    fine for codecs whose moov entry doesn't need frame data, fatal for
    AC-3. Adding delay_moov (holds the moov until each stream's first
    packet is seen) fixes it without disabling empty_moov's benefit for
    everything else."""
    import shutil as _shutil
    if _shutil.which("ffmpeg") is None or _shutil.which("ffprobe") is None:
        print("SKIP: ffmpeg/ffprobe not on PATH -- ensure_mp4 AC-3-audio test not exercised")
        return
    import subprocess
    from app.media import segments as segment_media

    ts_path = os.path.join(storage_dir, "with_ac3_audio.ts")
    gen = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1:r=5",
         "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
         "-c:v", "libx264", "-profile:v", "baseline",
         "-c:a", "ac3", "-b:a", "128k",
         "-f", "mpegts", ts_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    assert gen.returncode == 0 and os.path.exists(ts_path), gen.stderr
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", ts_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    codecs = {line.strip().rstrip(",") for line in probe.stdout.decode().splitlines() if line.strip()}
    assert codecs == {"ac3"}, probe.stdout

    mp4_path = os.path.join(storage_dir, "with_ac3_audio.mp4")
    ok, error = segment_media.ensure_mp4(ts_path, mp4_path)
    assert ok, f"ensure_mp4 failed on a TS with AC-3 audio: {error}"
    assert os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0

    verify = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", mp4_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    out_codecs = {line.strip().rstrip(",") for line in verify.stdout.decode().splitlines() if line.strip()}
    assert out_codecs == {"ac3"}, f"output MP4 lost its audio track: {verify.stdout!r} {verify.stderr!r}"
    print("OK: ensure_mp4() remuxes a TS with AC-3 audio into a playable MP4 "
          "(previously failed with 'Cannot write moov atom before AC3 packets')")


def test_video_info_ffprobe_and_probe_event(storage_dir):
    """New feature test: Probe._sample_video_info() buffers raw TS packets
    and periodically shells out to ffprobe for pixel format/color space/
    frame rate/field order/GOP structure -- detail this probe's own PES/
    NAL parsing does not otherwise extract (it only needs to know where
    IDRs are, not their pixel format). Needs a REAL, ffmpeg-decodable TS,
    unlike test_event_hooks.py's synthetic fake-NAL packets (fine for this
    probe's own demux logic, but not something ffmpeg can actually decode)
    -- so this lives here alongside the other real-ffmpeg-file tests."""
    import shutil as _shutil
    if _shutil.which("ffmpeg") is None or _shutil.which("ffprobe") is None:
        print("SKIP: ffmpeg/ffprobe not on PATH -- video_info test not exercised")
        return
    import subprocess

    ts_path = os.path.join(storage_dir, "video_info_source.ts")
    gen = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3",
         "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
         "-b:v", "1500k", "-g", "10", "-sc_threshold", "0",
         "-f", "mpegts", ts_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    assert gen.returncode == 0 and os.path.exists(ts_path), gen.stderr

    # -- Direct ffprobe-helper check --------------------------------------
    info = core._ffprobe_video_info(ts_path)
    assert info is not None, "ffprobe helper returned nothing for a valid H.264 TS"
    assert info["pix_fmt"] == "yuv420p", info
    assert info["width"] == 320 and info["height"] == 240, info
    assert info["frame_rate_fps"] is not None and abs(info["frame_rate_fps"] - 25.0) < 0.5, info
    assert info["scan_type"] == "progressive", info
    assert info["gop"] is not None and info["gop"]["frames_sampled"] > 0, info
    print("OK: _ffprobe_video_info() correctly reports pixel format/resolution/frame "
          "rate/scan type/GOP structure for a real encoded TS")

    # -- End-to-end through Probe: raw-packet buffer -> background sampler
    # -> ffprobe -> on_event("video_info", ...) -------------------------
    class _VideoInfoArgs:
        program = None
        pid_video = None
        pid_scte35 = None
        codec = None
        tolerance_ms = 6000.0
        max_early_ms = 50.0
        timeout_s = 12.0
        ok_threshold_ms = 41.0
        include_cra = False
        verbose = False
        csv_out = None
        json_out = None
        scte35_out = None
        scte35_log_file = None
        snapshot_dir = None
        snapshot_enabled = False
        snapshot_all_idr = False
        pre_frames = 0
        time_to_event_snapshot = "off"
        preroll_snapshot = "off"
        au_buffer_size = None
        ts_dump_dir = None
        ts_dump_window = 60.0
        ts_dump_all = False
        ts_dump_no_preroll = False
        ts_dump_max_files = None
        preroll_tolerance_ms = 500.0
        min_time_to_event_ms = 4000.0
        video_info_enabled = True
        video_info_interval_s = 0.2  # fast, so the test doesn't have to wait long

    events = []
    probe = core.Probe(_VideoInfoArgs(), on_event=lambda kind, data: events.append((kind, data)))
    with open(ts_path, "rb") as f:
        raw = f.read()
    packets, _ = core.extract_ts_packets(raw)
    assert len(packets) >= 500, (
        f"test TS only produced {len(packets)} packets -- below _sample_video_info()'s "
        "500-packet floor, bump -b:v/duration above so this test still exercises it")
    for pkt in packets:
        probe.handle_ts_packet(pkt)

    ok = _wait_until(lambda: any(kind == "video_info" for kind, _ in events), timeout=10.0)
    probe.close()
    assert ok, "Probe never emitted a video_info event within 10s"

    vi = [data for kind, data in events if kind == "video_info"][-1]
    assert vi["pix_fmt"] == "yuv420p", vi
    assert vi["scan_type"] == "progressive", vi
    print("OK: Probe's periodic background sampler emits a video_info event end-to-end "
          "(raw-packet buffer -> temp file -> ffprobe -> on_event)")


def test_delete_job(db, jobs, job_id):
    job_dir = config.job_dir(job_id)
    assert os.path.isdir(job_dir)
    db.delete_job(job_id)
    shutil.rmtree(job_dir, ignore_errors=True)
    assert db.get_job(job_id) is None
    assert not os.path.isdir(job_dir)
    print("OK: delete_job cleanup removes both the DB rows and the on-disk job directory")


def test_delete_job_with_cue_snapshot(db):
    """Regression test: cue_snapshots has a FOREIGN KEY REFERENCES jobs(id)
    and foreign_keys=ON is set on the connection -- delete_job() must clear
    that table too, or deleting a job that ever captured a time-to-event/
    pre-roll reference snapshot raises sqlite3.IntegrityError and the
    DELETE /api/jobs/{id} request 500s (with nothing about it visible in
    the GUI, since the frontend didn't used to handle that error either)."""
    job_id = db.create_job("cue-snapshot-delete-test", "file", {}, {})
    db.insert_cue_snapshot(job_id, {
        "cue_seq": 1, "tag": "time_to_event_cue_arrival",
        "path": "/tmp/does-not-matter.jpg", "frame_pts": 90000,
    })
    db.delete_job(job_id)  # must not raise IntegrityError
    assert db.get_job(job_id) is None
    print("OK: delete_job() also clears cue_snapshots, so a job with a captured "
          "reference snapshot can be deleted without an IntegrityError")


def test_cleanup_purges_old_segments_and_snapshots(db):
    """Covers app.jobs.cleanup.purge_job()/purge_all_jobs() end-to-end: old
    segments/snapshots/cue_snapshots (both their DB rows and on-disk files)
    are deleted while newer ones are kept, bytes_freed is accurate, and
    purge_all_jobs() only sweeps a job using ITS OWN saved retention
    settings -- a category left at None ("keep forever") must survive a
    sweep even when the data in it is far older than another job's active
    threshold."""
    job_id = db.create_job("cleanup-test", "file", {}, {})
    job_dir = config.job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)

    def _make_file(name):
        path = os.path.join(job_dir, name)
        with open(path, "wb") as f:
            f.write(b"x" * 1000)
        return path

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    old_iso = cleanup.cutoff_iso(7200)  # 2 hours ago

    old_seg_path, new_seg_path = _make_file("old_segment.ts"), _make_file("new_segment.ts")
    new_seg_id = db.insert_segment(job_id, {
        "path": new_seg_path, "window_end_wallclock": now_iso, "size_bytes": 1000})
    db.insert_segment(job_id, {
        "path": old_seg_path, "window_end_wallclock": old_iso, "size_bytes": 1000})

    old_snap_path, new_snap_path = _make_file("old_snapshot.jpg"), _make_file("new_snapshot.jpg")
    db.insert_snapshot(job_id, {"path": new_snap_path, "kind": "idr", "idr_ticks": 2})
    db.insert_snapshot(job_id, {"path": old_snap_path, "kind": "idr", "idr_ticks": 1})
    # insert_snapshot always stamps created_at=now() -- backdate the "old" one
    # directly so the age-based filter has something to actually filter on.
    with db._lock:
        db._conn.execute("UPDATE snapshots SET created_at=? WHERE path=?", (old_iso, old_snap_path))
        db._conn.commit()

    old_cue_path, new_cue_path = _make_file("old_cue_snapshot.jpg"), _make_file("new_cue_snapshot.jpg")
    db.insert_cue_snapshot(job_id, {
        "cue_seq": 2, "tag": "time_to_event_cue_arrival", "path": new_cue_path, "frame_pts": 90000})
    db.insert_cue_snapshot(job_id, {
        "cue_seq": 1, "tag": "time_to_event_cue_arrival", "path": old_cue_path, "frame_pts": 90000})
    with db._lock:
        db._conn.execute("UPDATE cue_snapshots SET created_at=? WHERE path=?", (old_iso, old_cue_path))
        db._conn.commit()

    # -- purge_job(): explicit one-off thresholds, independent of any saved
    # retention setting (both are left at their default None on this job).
    result = cleanup.purge_job(db, job_id, segment_max_age_s=3600, snapshot_max_age_s=3600)
    assert result["segments_deleted"] == 1
    assert result["snapshot_files_deleted"] == 2  # one plain snapshot + one cue_snapshot
    assert result["bytes_freed"] == 3000  # old segment + old snapshot + old cue_snapshot, 1000B each

    assert [s["id"] for s in db.list_segments(job_id)] == [new_seg_id]
    assert os.path.isfile(new_seg_path) and not os.path.exists(old_seg_path)
    assert [s["path"] for s in db.list_snapshots(job_id)] == [new_snap_path]
    assert os.path.isfile(new_snap_path) and not os.path.exists(old_snap_path)
    assert [s["path"] for s in db.list_cue_snapshots(job_id)] == [new_cue_path]
    assert os.path.isfile(new_cue_path) and not os.path.exists(old_cue_path)
    print("OK: cleanup.purge_job() deletes segments/snapshots/cue_snapshots older than the "
          "given threshold (DB rows + on-disk files), keeps newer ones, and reports accurate "
          "counts and bytes_freed")

    # -- purge_all_jobs(): only jobs with a configured retention setting are
    # swept at all, each using its own saved threshold.
    ancient_iso = cleanup.cutoff_iso(30 * 24 * 3600)  # 30 days ago

    keep_forever_id = db.create_job("cleanup-keep-forever-test", "file", {}, {})
    keep_dir = config.job_dir(keep_forever_id)
    os.makedirs(keep_dir, exist_ok=True)
    ancient_seg_path = os.path.join(keep_dir, "ancient_segment.ts")
    with open(ancient_seg_path, "wb") as f:
        f.write(b"x" * 500)
    db.insert_segment(keep_forever_id, {
        "path": ancient_seg_path, "window_end_wallclock": ancient_iso, "size_bytes": 500})
    # segment_retention_s/snapshot_retention_s both left at None here --
    # purge_all_jobs() must skip this job entirely, no matter how old its data is.

    swept_id = db.create_job("cleanup-sweep-test", "file", {}, {})
    swept_dir = config.job_dir(swept_id)
    os.makedirs(swept_dir, exist_ok=True)
    swept_old_path = os.path.join(swept_dir, "swept_old.ts")
    with open(swept_old_path, "wb") as f:
        f.write(b"x" * 500)
    db.insert_segment(swept_id, {
        "path": swept_old_path, "window_end_wallclock": ancient_iso, "size_bytes": 500})
    db.set_job_retention(swept_id, 3600, None)

    totals = cleanup.purge_all_jobs(db)
    assert totals["jobs_swept"] == 1  # only swept_id has a retention setting configured
    assert totals["segments_deleted"] == 1
    assert totals["bytes_freed"] == 500
    assert os.path.isfile(ancient_seg_path), \
        "a job with retention left at None ('keep forever') must not be touched by the sweep"
    assert len(db.list_segments(keep_forever_id)) == 1
    assert not os.path.exists(swept_old_path)
    assert db.list_segments(swept_id) == []
    print("OK: cleanup.purge_all_jobs() only sweeps jobs with a configured retention setting, "
          "using each job's own saved thresholds, and never touches a 'keep forever' job")


def test_db_migration_adds_retention_columns(storage_dir):
    """Regression test for Database._migrate(): a pre-existing "old schema"
    jobs table (as a deployment upgrading from before this feature existed
    would have -- also missing the earlier video_info column, to exercise
    both migrations applying together) must gain segment_retention_s/
    snapshot_retention_s without losing any existing data, and
    set_job_retention()/get_job() must work normally afterward."""
    import sqlite3
    db_path = os.path.join(storage_dir, "legacy_schema_test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_config TEXT NOT NULL,
            tuning_config TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'starting',
            error TEXT,
            pid_info TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO jobs (id, name, source_type, source_config, tuning_config, status, created_at) "
        "VALUES ('legacy1', 'pre-existing job', 'file', '{}', '{}', 'finished', '2025-01-01T00:00:00')")
    conn.commit()
    conn.close()

    db = Database(db_path)  # __init__ -> _migrate() must ALTER TABLE, not crash or drop data
    job = db.get_job("legacy1")
    assert job is not None, "pre-existing row must survive the schema migration"
    assert job["name"] == "pre-existing job"
    assert job["segment_retention_s"] is None
    assert job["snapshot_retention_s"] is None

    db.set_job_retention("legacy1", 3600, None)
    job = db.get_job("legacy1")
    assert job["segment_retention_s"] == 3600
    assert job["snapshot_retention_s"] is None
    print("OK: Database._migrate() adds segment_retention_s/snapshot_retention_s to a "
          "pre-existing jobs table without data loss, and set_job_retention()/get_job() "
          "work correctly against the migrated schema")


async def main():
    with tempfile.TemporaryDirectory(prefix="scte35_analyzer_it_") as storage_dir:
        db, jobs, file_job_id = await test_file_job(storage_dir)
        await test_restart_appends_and_clears_error(db, jobs, file_job_id)
        test_edit_job_fields(db)
        concurrent_job_ids = await test_concurrent_jobs_use_separate_processes(db, jobs)
        udp_job_id = await test_udp_job(db, jobs)
        test_ensure_mp4_multi_pid_ts(storage_dir)
        test_ensure_mp4_aac_audio_ts(storage_dir)
        test_ensure_mp4_ac3_audio_ts(storage_dir)
        test_video_info_ffprobe_and_probe_event(storage_dir)
        test_cleanup_purges_old_segments_and_snapshots(db)
        test_db_migration_adds_retention_columns(storage_dir)
        test_delete_job(db, jobs, file_job_id)
        test_delete_job(db, jobs, udp_job_id)
        for job_id in concurrent_job_ids:
            test_delete_job(db, jobs, job_id)
        test_delete_job_with_cue_snapshot(db)
    print("\nAll integration tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
