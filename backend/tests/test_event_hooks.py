#!/usr/bin/env python3
"""
Regression test for the scte35-analyzer addition to app/core/probe.py: the
on_event(kind, data) hook that the job manager/WebSocket layer relies on to
stream live markers to the browser.

This deliberately does NOT touch the original matching/demux logic (that is
covered end-to-end by test_engine_offline.py, an unmodified copy of the
upstream scte35_idr_diff project's own test suite, run against this fork to
confirm the refactor changed no behavior). This file only asserts that each
event kind actually fires, with the fields the GUI backend/frontend depend
on present: pat_info, pid_info, scte35_cue, match_result, snapshot_saved,
segment_saved.

Run: python3 test_event_hooks.py
"""
import os
import shutil
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.core import probe as mod  # noqa: E402

# Reuse the synthetic-packet builders from the offline engine test instead
# of duplicating them.
sys.path.insert(0, os.path.dirname(__file__))
import test_engine_offline as helpers  # noqa: E402


class Args:
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
    snapshot_enabled = True
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


def test_pat_pmt_events():
    events = []
    probe = mod.Probe(Args(), on_event=lambda kind, data: events.append((kind, data)))

    pmt_pid, video_pid, scte35_pid = 0x100, 0x101, 0x1F0
    probe.handle_ts_packet(helpers.build_pat(program_number=1, pmt_pid=pmt_pid))
    cuei = bytes([0x05, 0x04]) + b"CUEI"
    probe.handle_ts_packet(helpers.build_pmt(
        pcr_pid=video_pid,
        streams=[(mod.STREAM_TYPE_H264, video_pid, b""),
                 (mod.STREAM_TYPE_SCTE35, scte35_pid, cuei)],
        pmt_pid=pmt_pid,
    ))

    kinds = [k for k, _ in events]
    assert "pat_info" in kinds, kinds
    assert "pid_info" in kinds, kinds
    pid_info = next(d for k, d in events if k == "pid_info")
    assert pid_info["video_pid"] == video_pid
    assert pid_info["video_codec"] == "h264"
    assert pid_info["scte35_pid"] == scte35_pid
    print("OK: on_event fires pat_info and pid_info with the discovered PIDs/codec")
    return probe


def test_scte35_cue_and_match_result_events():
    events = []
    probe = mod.Probe(Args(), on_event=lambda kind, data: events.append((kind, data)))
    probe.video_pid = 0x101
    probe.video_codec = "h264"
    probe.scte35_pid = 0x1F0

    target_pts_seconds = 100.0
    SpliceInsert = type("SpliceInsert", (), {})
    fake_command = SpliceInsert()
    fake_command.pts_time = target_pts_seconds
    fake_command.splice_event_id = 4242
    fake_command.out_of_network_indicator = True
    fake_info = types.SimpleNamespace(pts_adjustment=0)
    fake_cue = types.SimpleNamespace(command=fake_command, info_section=fake_info, descriptors=[])

    # _log_full_cue + _register_cue is what handle_scte35() calls once a
    # section has been decoded by threefive3 -- called directly here since
    # this environment has no threefive3 install, same workaround the
    # upstream offline test suite uses.
    probe._log_full_cue(1, fake_cue, b"\x00" * 16)
    probe._register_cue(fake_cue, 1)

    assert any(k == "scte35_cue" for k, _ in events), [k for k, _ in events]
    cue_event = next(d for k, d in events if k == "scte35_cue")
    assert cue_event["cue_seq"] == 1
    assert cue_event["command_type"] == "SpliceInsert"
    print("OK: on_event fires scte35_cue with the full decoded-cue record")

    idr_pts_ticks = int(round((target_pts_seconds + 0.020) * mod.PTS_HZ)) % mod.PTS_MAX
    es_payload = helpers.h264_idr_nal()
    packets = helpers.build_pes_packets(probe.video_pid, idr_pts_ticks, es_payload)
    packets += helpers.build_pes_packets(
        probe.video_pid, (idr_pts_ticks + 3600) % mod.PTS_MAX, es_payload,
        cc_start=len(packets) % 16)
    for pkt in packets:
        probe.handle_ts_packet(pkt)

    assert any(k == "match_result" for k, _ in events), [k for k, _ in events]
    match_event = next(d for k, d in events if k == "match_result")
    assert match_event["event_id"] == 4242
    assert match_event["verdict"] == "OK", match_event["verdict"]
    assert abs(match_event["delta_ms"] - 20.0) < 1.0
    print("OK: on_event fires match_result with the correct verdict/delta_ms "
          "for a matched SCTE-35/IDR pair")


def test_snapshot_saved_event():
    if shutil.which("ffmpeg") is None:
        print("SKIP: ffmpeg not on PATH -- snapshot_saved event not exercised")
        return
    import subprocess
    # A real ffmpeg-encoded frame, same technique test_engine_offline.py
    # uses -- the synthetic placeholder NAL used elsewhere in this file is
    # fine for exercising the PTS-matching logic, but isn't valid enough
    # for ffmpeg to actually decode.
    gen = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=blue:s=64x64:d=1:r=1", "-frames:v", "1",
         "-c:v", "libx264", "-profile:v", "baseline", "-f", "h264", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    assert gen.returncode == 0 and gen.stdout, gen.stderr
    full_au = gen.stdout

    with tempfile.TemporaryDirectory() as snap_dir:
        events = []
        args = Args()
        args.snapshot_dir = snap_dir
        args.snapshot_all_idr = True
        probe = mod.Probe(args, on_event=lambda kind, data: events.append((kind, data)))
        probe.video_codec = "h264"

        pts = int(1000.0 * mod.PTS_HZ)
        probe._enqueue_snapshot(pts, full_au)
        probe.close()  # drains the background snapshot worker

        assert any(k == "snapshot_saved" for k, _ in events), [k for k, _ in events]
        snap_event = next(d for k, d in events if k == "snapshot_saved")
        assert os.path.exists(snap_event["path"]), snap_event
        print("OK: on_event fires snapshot_saved once the IDR JPEG is actually on disk")


def _make_fake_cue(pts_time, event_id):
    """Same minimal stand-in for a threefive3-decoded cue used throughout
    this file and test_engine_offline.py (no threefive3 install in this
    environment)."""
    SpliceInsert = type("SpliceInsert", (), {})
    fake_command = SpliceInsert()
    fake_command.pts_time = pts_time
    fake_command.splice_event_id = event_id
    fake_command.out_of_network_indicator = True
    fake_info = types.SimpleNamespace(pts_adjustment=0)
    return types.SimpleNamespace(command=fake_command, info_section=fake_info, descriptors=[])


def _gen_multiframe_h264(num_frames=10, gop_size=5):
    """A real, multi-frame, decodable H.264 elementary stream (no B-frames,
    so decode order == PTS order, one AUD-delimited access unit per frame)
    -- the same technique test_engine_offline.py's test_pre_frame_snapshots
    uses, factored out here since the reference-snapshot tests below need
    the same kind of real GOP to decode an arbitrary (non-IDR) frame from.
    Returns (access_units, fps_ticks) or (None, None) if ffmpeg is missing."""
    if shutil.which("ffmpeg") is None:
        return None, None
    import subprocess
    gen = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc=size=64x64:rate=1:duration={num_frames}",
         "-frames:v", str(num_frames), "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-profile:v", "baseline",
         "-g", str(gop_size), "-sc_threshold", "0", "-bf", "0",
         "-x264-params", "aud=1", "-f", "h264", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    assert gen.returncode == 0 and gen.stdout, (
        "failed to generate a real multi-frame H.264 test sequence: %s" % gen.stderr)
    aus = helpers._split_h264_access_units(gen.stdout)
    assert len(aus) == num_frames, (
        "expected %d access units, split out %d" % (num_frames, len(aus)))
    return aus, mod.PTS_HZ  # 1 fps source -> PTS_HZ ticks between frames


def test_time_to_event_reference_snapshots():
    """time_to_event_snapshot's two modes, both exercised against the same
    real multi-frame GOP: cue_arrival_frame (the frame on air the instant
    the cue is registered) and target_pts_frame (the frame at the cue's own
    literal target PTS, captured once that PTS is actually observed)."""
    aus, fps_ticks = _gen_multiframe_h264()
    if aus is None:
        print("SKIP: ffmpeg not on PATH -- time_to_event reference-snapshot modes not exercised")
        return
    pts_list = [i * fps_ticks for i in range(len(aus))]

    # -- cue_arrival_frame -------------------------------------------------
    with tempfile.TemporaryDirectory() as snap_dir:
        events = []
        args = Args()
        args.snapshot_dir = snap_dir
        args.time_to_event_snapshot = "cue_arrival_frame"
        probe = mod.Probe(args, on_event=lambda kind, data: events.append((kind, data)))
        probe.video_codec = "h264"

        # Frames 0..3 are "on air" already; the cue arrives right after
        # frame 3, so that frame is what "on air when the cue arrived"
        # should mean.
        for i in range(4):
            probe._process_video_pes((pts_list[i], None, aus[i]))
        fake_cue = _make_fake_cue(pts_time=(pts_list[-1] + fps_ticks) / mod.PTS_HZ, event_id=1)
        probe._register_cue(fake_cue, 1)
        probe.snapshot_queue.join()
        probe.close()

        ref_events = [d for k, d in events if k == "reference_snapshot_saved"]
        arrival = next((d for d in ref_events if d["tag"] == "time_to_event_cue_arrival"), None)
        assert arrival is not None, ref_events
        assert arrival["frame_pts"] == pts_list[3], arrival
        assert os.path.exists(arrival["path"]) and os.path.getsize(arrival["path"]) > 0
        with open(arrival["path"], "rb") as f:
            assert f.read(2) == b"\xff\xd8"
        print("OK: time_to_event_snapshot='cue_arrival_frame' captures the frame that "
              "was actually on air the instant the cue was registered")

    # -- target_pts_frame ---------------------------------------------------
    with tempfile.TemporaryDirectory() as snap_dir:
        events = []
        args = Args()
        args.snapshot_dir = snap_dir
        args.time_to_event_snapshot = "target_pts_frame"
        probe = mod.Probe(args, on_event=lambda kind, data: events.append((kind, data)))
        probe.video_codec = "h264"

        # A little live context before the cue arrives, then register a cue
        # whose target PTS lands exactly on frame 5 (not yet seen).
        for i in range(2):
            probe._process_video_pes((pts_list[i], None, aus[i]))
        fake_cue = _make_fake_cue(pts_time=pts_list[5] / mod.PTS_HZ, event_id=2)
        probe._register_cue(fake_cue, 1)
        # Now the rest of the stream arrives, including frame 5 itself.
        for i in range(2, len(aus)):
            probe._process_video_pes((pts_list[i], None, aus[i]))
        probe.snapshot_queue.join()
        probe.close()

        ref_events = [d for k, d in events if k == "reference_snapshot_saved"]
        target = next((d for d in ref_events if d["tag"] == "time_to_event_target_pts"), None)
        assert target is not None, ref_events
        assert target["frame_pts"] == pts_list[5], target
        assert os.path.exists(target["path"]) and os.path.getsize(target["path"]) > 0
        print("OK: time_to_event_snapshot='target_pts_frame' captures the frame "
              "actually observed at the cue's own literal target PTS")


def test_preroll_reference_snapshots():
    """preroll_snapshot's two modes: same_as_matched_idr (relabels the
    already-captured matched-IDR snapshot, no extra decode) and
    realtime_deadline_frame (a wall-clock threading.Timer capture)."""
    if shutil.which("ffmpeg") is None:
        print("SKIP: ffmpeg not on PATH -- preroll reference-snapshot modes not exercised")
        return
    import subprocess

    def _one_real_frame():
        gen = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=blue:s=64x64:d=1:r=1", "-frames:v", "1",
             "-c:v", "libx264", "-profile:v", "baseline", "-f", "h264", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        assert gen.returncode == 0 and gen.stdout, gen.stderr
        return gen.stdout

    # -- same_as_matched_idr -------------------------------------------------
    with tempfile.TemporaryDirectory() as snap_dir:
        events = []
        args = Args()
        args.snapshot_dir = snap_dir
        args.preroll_snapshot = "same_as_matched_idr"
        probe = mod.Probe(args, on_event=lambda kind, data: events.append((kind, data)))
        probe.video_pid = 0x101
        probe.video_codec = "h264"

        target_pts_seconds = 200.0
        fake_cue = _make_fake_cue(pts_time=target_pts_seconds, event_id=3)
        probe._register_cue(fake_cue, 1)

        idr_pts_ticks = int(round(target_pts_seconds * mod.PTS_HZ)) % mod.PTS_MAX
        full_au = _one_real_frame()
        # A real ES payload has more than one NAL (SPS/PPS/AUD/slice), but
        # PesReassembler (like any unbounded-length video PES) only flushes
        # a PES once the NEXT one's PUSI arrives -- so a second, throwaway
        # frame has to follow for the IDR's own PES to actually get parsed
        # and matched, same as test_scte35_cue_and_match_result_events.
        packets = helpers.build_pes_packets(probe.video_pid, idr_pts_ticks, full_au)
        packets += helpers.build_pes_packets(
            probe.video_pid, (idr_pts_ticks + 3600) % mod.PTS_MAX, full_au,
            cc_start=len(packets) % 16)
        for pkt in packets:
            probe.handle_ts_packet(pkt)
        probe.snapshot_queue.join()
        probe.close()

        match_event = next(d for k, d in events if k == "match_result")
        assert match_event["snapshot_path"], match_event
        ref_events = [d for k, d in events if k == "reference_snapshot_saved"]
        same_as = next((d for d in ref_events if d["tag"] == "preroll_same_as_matched_idr"), None)
        assert same_as is not None, ref_events
        assert same_as["path"] == match_event["snapshot_path"], (same_as, match_event)
        print("OK: preroll_snapshot='same_as_matched_idr' relabels the already-"
              "captured matched-IDR snapshot instead of decoding a second copy")

    # -- realtime_deadline_frame ---------------------------------------------
    with tempfile.TemporaryDirectory() as snap_dir:
        events = []
        args = Args()
        args.snapshot_dir = snap_dir
        args.preroll_snapshot = "realtime_deadline_frame"
        probe = mod.Probe(args, on_event=lambda kind, data: events.append((kind, data)))
        probe.video_codec = "h264"

        full_au = _one_real_frame()
        on_air_pts = int(500.0 * mod.PTS_HZ)
        probe._process_video_pes((on_air_pts, None, full_au))

        # A cue whose declared time_to_event is a tiny 80ms in the future --
        # short enough for this test to just wait it out.
        target_pts_seconds = (on_air_pts / mod.PTS_HZ) + 0.08
        fake_cue = _make_fake_cue(pts_time=target_pts_seconds, event_id=4)
        probe._register_cue(fake_cue, 1)

        time.sleep(0.5)  # let the threading.Timer fire and enqueue the snapshot
        probe.snapshot_queue.join()
        probe.close()

        ref_events = [d for k, d in events if k == "reference_snapshot_saved"]
        deadline = next((d for d in ref_events if d["tag"] == "preroll_realtime_deadline"), None)
        assert deadline is not None, ref_events
        assert deadline["frame_pts"] == on_air_pts, deadline
        assert os.path.exists(deadline["path"]) and os.path.getsize(deadline["path"]) > 0
        print("OK: preroll_snapshot='realtime_deadline_frame' captures the frame on "
              "air at the cue's real wall-clock deadline via a threading.Timer")


def test_segment_saved_event():
    with tempfile.TemporaryDirectory() as dump_dir:
        events = []
        args = Args()
        args.ts_dump_dir = dump_dir
        args.ts_dump_window = 0.05  # tiny window so the test runs fast
        probe = mod.Probe(args, on_event=lambda kind, data: events.append((kind, data)))
        probe.video_pid = 0x101
        probe.scte35_pid = 0x1F0

        pkt = helpers.build_ts_packet(probe.scte35_pid, b"\x00" * 184, pusi=False, cc=0)
        probe.handle_ts_packet(pkt)
        probe._ts_dump_note_event(1)
        time.sleep(0.1)
        probe.handle_ts_packet(pkt)  # crosses the window boundary -> rotates
        probe.close()

        assert any(k == "segment_saved" for k, _ in events), [k for k, _ in events]
        seg_event = next(d for k, d in events if k == "segment_saved")
        assert os.path.exists(seg_event["path"]), seg_event
        assert seg_event["event_count"] >= 1
        print("OK: on_event fires segment_saved with a real .ts file on disk and its cue_seq list")


if __name__ == "__main__":
    test_pat_pmt_events()
    test_scte35_cue_and_match_result_events()
    test_snapshot_saved_event()
    test_time_to_event_reference_snapshots()
    test_preroll_reference_snapshots()
    test_segment_saved_event()
    print("\nAll event-hook tests passed.")
