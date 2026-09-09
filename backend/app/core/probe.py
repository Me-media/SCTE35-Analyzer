#!/usr/bin/env python3
"""
scte35_idr_diff.py
Version: 0.10

Measures the PTS delta between SCTE-35 splice points (splice_insert /
time_signal, program-level splice_time) carried in a SCTE-35 PID and the
nearest IDR (video I-frame) access unit PTS in a video PID, read live from
a multicast MPEG-TS stream (UDP, raw or RTP-encapsulated).

Why this exists:
  SCTE-35 splice points are only cleanly executable by downstream splicers
  if the target PTS lands on (or very close to) a video IDR frame. Encoders
  that do not force an IDR at the splice point produce cue messages that
  cannot be spliced cleanly -> visible glitches / black frames on ad
  insertion. This tool quantifies that alignment error in milliseconds,
  live, off a multicast feed, for headend QC and encoder acceptance testing.

Dependencies:
  pip install threefive3 --break-system-packages
  (threefive3 is used only to decode SCTE-35 splice_info_section payloads
  once we have located and reassembled them from the TS ourselves; all
  MPEG-TS / RTP / PES / NAL demuxing below is custom and has no other
  external dependency.)

Design notes / important caveats (read before treating output as ground
truth in a compliance report):

  1. pts_adjustment: per SCTE-35 (ANSI/SCTE 35), any pts_time found in a
     splice command must have splice_info_section.pts_adjustment ADDED to
     it (modulo 2^33) to get the true intended PTS, because devices that
     relay a cue are allowed to adjust it. threefive does NOT apply this
     automatically (verified against the library source) -- this script
     applies it explicitly. If your headend never re-stamps cues upstream
     of this probe, pts_adjustment will normally be 0 and this is moot.
     IMPORTANT: threefive3's SpliceInfoSection.pts_adjustment is exposed
     as SECONDS (a float), not raw 90kHz ticks -- the library reads the
     raw 33-bit field and immediately divides it by 90000 via its own
     as_90k() helper before handing it to callers (verified against
     threefive3/threefive source, Sept 2026). _register_cue() below
     converts it back to ticks explicitly; treating it as already-ticks
     was a real bug present through v0.10.0 that silently applied almost
     no correction (a couple of ticks, i.e. microseconds) instead of the
     intended multi-second shift whenever pts_adjustment was nonzero.

  2. Access-unit/PES alignment: this tool assumes one PES packet == one
     video access unit, which holds for essentially all broadcast-profile
     encoders. If an access unit is split across PES packets (unusual),
     the reported IDR PTS is still correct because it is read from the
     PES header of the packet carrying the first NAL unit of that AU.

  3. HEVC CRA vs IDR: some encoders use CRA (NAL type 21) as the splice
     point instead of true IDR (19/20). CRA pictures are not fully
     independent (leading pictures may reference before the CRA), so they
     are reported separately unless --include-cra is set. Treat a
     CRA-based splice point as "needs review", not automatically OK.

  4. PTS wraparound: the 90kHz PTS counter wraps every ~26.5h (2^33
     ticks). Matching logic below tolerates this within the matching
     window but a splice point queued for longer than the window across
     a wrap could mismatch. Not a concern for a live probe restarted
     periodically.

  5. This is a diagnostic/QC tool, not a certified SCTE-35 conformance
     tester. Validate findings against a reference tool (e.g. TSDuck's
     `tsp -P scte35`) before escalating to a vendor.

  6. time_to_event_ms / actual_preroll_ms: time_to_event_ms is a PTS-domain
     figure (target PTS minus the most recently observed video PTS at the
     moment the cue is registered) -- it approximates "how far in the
     future, per the transport stream's own clock, was this splice
     signaled", the same concept a PCR/PTS-correlating analyzer computes,
     but WITHOUT parsing PCR: it uses the nearest preceding video AU's PTS
     instead of a true STC interpolation, so it carries a small error
     bounded by roughly one video frame interval. actual_preroll_ms is a
     WALL-CLOCK figure (real elapsed time, via a monotonic clock, between
     registering the cue and observing the matching IDR) -- it is only
     meaningful if the feed is arriving in genuine real time (a live
     encoder/multicast feed; NOT a file replayed faster/slower than real
     time, which would make actual_preroll_ms meaningless). ANSI/SCTE 35
     itself does NOT require these two to be equal, nor does it define a
     tolerance for that gap -- preroll_delta_ms/PREROLL_SHORT is this
     tool's own operational check, not a cited standards requirement. A
     large negative preroll_delta_ms (actual well below declared) is
     therefore not itself a standards violation, but IS operationally
     significant: it means downstream ad-decisioning/splicing equipment
     got materially less real reaction time than the cue's own timing
     implied.

  7. signal_verdict / --min-time-to-event-ms: unlike preroll_delta_ms
     above, SCTE-35 DOES appear to specify a minimum advance-notice
     requirement for the message itself -- per secondary sources citing
     ANSI/SCTE 35 (2019) sections 9.2 (splice_insert) and 10.3.3
     (time_signal): "sent at least once a minimum of 4 seconds in advance
     of the desired splice time" (corroborated independently by ETSI TS
     103 752-1 clause 7.2, which cites the same 4-second SCTE-35 minimum).
     This has NOT been verified against the primary ANSI/SCTE 35 text
     itself (access to the full standard was not available when this was
     written) -- treat the citation as secondary-source-corroborated, not
     primary-verified. signal_verdict=SIGNAL_LATE flags the FIRST
     registered occurrence of a splice_event_id whose time_to_event_ms is
     below --min-time-to-event-ms (default 4000). Later retransmissions of
     the same event_id (common practice, for resilience against packet
     loss) are reported as RETRANSMISSION rather than re-evaluated, since
     the 4-second requirement is about the initial signal, not every
     repeat as the splice point gets closer -- if your encoder does not
     retransmit, or reuses splice_event_id across genuinely distinct
     events, this dedup heuristic may need revisiting.

  8. --input-file (file mode): the file is read once, start to finish, as
     fast as disk I/O allows -- NOT paced to whatever real-time cadence the
     stream originally had. This makes actual_preroll_ms/preroll_verdict
     meaningless in file mode (there is no "real time" being measured, only
     however long this process took to read the file), and means
     --timeout-s's normal live-capture semantics (wait up to N seconds for
     a matching IDR) do not apply the way they do live: reaching end of
     file with a splice point still pending is treated as conclusive --
     "no matching IDR ever arrived" -- and reported as MISSED immediately
     (see flush_pending_as_missed()), regardless of --timeout-s. Everything
     else (PID/codec auto-detection, PTS-domain matching, time_to_event_ms,
     signal_verdict, CSV/JSON schema) works identically to live capture.
     See "File input mode" in the README.

  9. gop_verdict: a downstream packager/splicer can only cut a clean
     ad-break transition on an actual IDR -- it cannot invent a cut point
     that doesn't exist in the encoded stream. So the question that
     actually explains "ads start later than they should" is not
     actual_preroll_ms (item 6, a wall-clock probe-side figure) but
     whether the ENCODER forced a real IDR at (or very near) target_pts,
     as opposed to just leaving its normal GOP cadence running and
     letting the packager fall through to whatever IDR happened to come
     next. gop_verdict distinguishes these by comparing delta_ms against
     the stream's own average GOP duration (periodically resampled via
     ffprobe -- see video_info/_sample_video_info, avg_gop_length_frames /
     frame_rate_fps): FORCED (delta_ms is small -- already verdict=OK --
     consistent with a genuine forced keyframe at the splice point, so any
     remaining lateness is downstream of this probe, e.g. the
     packager/ad-decisioning layer); GOP_WAIT (delta_ms lands within
     ~20%/200ms of a whole multiple of the GOP duration -- the encoder
     most likely never forced a keyframe at all, and the packager just cut
     at its next naturally-scheduled IDR instead -- this IS the root cause
     to chase for a systematically-late ad start); UNCLEAR (neither
     pattern fits -- e.g. a variable-GOP encoder, or delta_ms too large/odd
     to attribute to simple GOP cadence); N/A (a MISSED cue, or no GOP
     sample has landed yet -- requires --video-info-enabled in the GUI, see
     README). Heuristic, not a standards-defined figure: it infers encoder
     behavior from timing statistics, it does not inspect the encoder's
     own keyframe-forcing decision directly.

  10. near_miss_ms / closest_rejected_diff_ms: a MISSED verdict on its own
      doesn't say whether the probe saw nothing IDR-like anywhere near the
      cue's target PTS, or whether it saw a candidate IDR that just fell
      outside the accepted matching window (-max_early_s .. +match_window_s,
      see _match_idr) -- an important distinction when comparing an
      incoming feed against its packaged/outgoing counterpart, since
      "SCTE-35 sometimes goes missing on the incoming side" can mean either
      "the encoder never gave us a usable IDR" (a real gap) or "an IDR
      arrived, just too early/late to count" (a tolerance/jitter problem,
      not a lost event). _match_idr() now tracks, for every still-pending
      cue, the closest out-of-window IDR it has seen so far
      (closest_rejected_diff_ms on the pending entry -- signed ms, positive
      = the candidate arrived late, negative = early); if the cue is
      eventually reported MISSED, that figure both extends the verdict
      string (e.g. "MISSED (no IDR near target PTS within timeout -- nearest
      IDR seen was 340ms too late to match, outside the accepted window)")
      and rides along unchanged as its own near_miss_ms field in the
      CSV/JSON/DB/GUI schema (None when no rejected candidate was ever seen
      for that cue, including for every non-MISSED verdict). Purely a
      diagnostic on top of the existing --tolerance-ms/--max-early-ms
      matching logic -- it does not change what counts as a match.

Usage examples:
  # Raw UDP multicast, auto-detect video codec + PIDs from PAT/PMT
  sudo python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000

  # RTP-encapsulated multicast, explicit interface, CSV log
  sudo python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \\
      --iface 10.0.0.5 --transport rtp --csv-out splice_report.csv

  # Manual PID override (no CUEI registration descriptor in PMT)
  python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \\
      --pid-video 0x101 --pid-scte35 0x1F0

  # Local testing against loopback, no real multicast network needed --
  # --addr outside 224.0.0.0-239.255.255.255 is treated as plain unicast
  # UDP (no group join attempted), so this "just works" against e.g. an
  # ffmpeg/tsp test stream aimed at 127.0.0.1:
  python3 scte35_idr_diff.py --addr 127.0.0.1 --port 5000

  # Reprocess a captured file offline instead of a live feed -- e.g. a
  # --ts-dump-dir capture from an earlier incident, or any standard
  # 188-byte-aligned .ts file. Runs once start to finish, then exits on
  # its own (no --duration/Ctrl-C needed). See caveat 8 above for what
  # this does NOT tell you (actual_preroll_ms/preroll_verdict, timing):
  python3 scte35_idr_diff.py --input-file capture.ts --csv-out report.csv

  # Capture EVERYTHING SCTE-35 related (all commands, all descriptor
  # fields, including splice_null/canceled/immediate messages that never
  # get matched against an IDR) into its own files, alongside the normal
  # console output and match/miss CSV:
  python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \\
      --csv-out splice_report.csv \\
      --scte35-out scte35_full.jsonl --scte35-log-file scte35.log

Output channels (independent of each other, enable any combination):
  console            Lifecycle (PAT/PMT/join) + match/miss verdicts (this
                      is what you see by default; unaffected by the flags
                      below unless --scte35-log-file is also set, in which
                      case the per-cue detail lines move off the console
                      and into that file instead, to avoid drowning out
                      the verdict lines on a busy SCTE-35 PID).
  --csv-out FILE      One row per match/miss OUTCOME (tabular, for
                      spreadsheets/Grafana/etc).
  --json-out FILE     Same outcomes as --csv-out, as JSON-lines.
  --scte35-out FILE   EVERY decoded SCTE-35 message, in full -- every
                      field threefive parsed, every descriptor (e.g. the
                      segmentation_descriptor fields that carry a
                      time_signal() cue's actual event identity), as
                      JSON-lines. Independent of whether the message had a
                      time-specified pts_time at all, so this is the one
                      to reach for when you need the complete raw SCTE-35
                      picture rather than just the IDR-alignment verdict.
                      Join to --json-out/--csv-out rows via "cue_seq".
  --scte35-log-file FILE   Same per-cue information as --scte35-out, but
                      as a human-readable text log line (one line per
                      SCTE-35 message) instead of structured JSON -- for
                      tailing/grepping without a JSON parser.
  --snapshot-dir DIR  Save a JPEG of the matched IDR access unit for each
                      splice event (requires ffmpeg on PATH -- decodes just
                      that one access unit standalone). Referenced from the
                      --csv-out/--json-out rows and the console verdict
                      line as "snapshot_path", so you can visually confirm
                      what the splice frame actually looked like (clean
                      program/ad content vs. a black frame, corruption,
                      etc.) instead of trusting the PTS math alone.
  --pre-frames N      With --snapshot-dir, also save the N access units
                      immediately BEFORE the matched IDR, in DISPLAY (PTS)
                      order, as additional JPEGs -- so you can see the
                      actual visual transition into the splice point, not
                      just the IDR itself. Since P/B frames are not
                      independently decodable, this buffers raw access
                      units (up to --au-buffer-size of them) and decodes
                      the whole GOP chunk back to the previous IDR/CRA once
                      per snapshot, then keeps only the frames asked for.
                      Referenced from --csv-out/--json-out/console as
                      "pre_frame_snapshot_paths" (oldest first), while
                      "snapshot_path" keeps meaning the IDR's own frame.
  --ts-dump-dir DIR   Continuously chop the RAW, byte-exact multicast feed
                      (every PID -- not just video/SCTE-35, so the file is
                      a proper standalone .ts playable in VLC/ffplay or
                      loadable in TSDuck) into fixed-length windows
                      (--ts-dump-window), and only WRITE a window to disk
                      if at least one SCTE-35 message was decoded during
                      it -- so you get a raw capture around every actual
                      SCTE-35 event for offline forensic analysis, without
                      recording 24/7. By default the immediately preceding
                      window is prepended too (--ts-dump-no-preroll to
                      disable), so an event near the start of a window
                      still has real pre-roll context instead of an
                      abrupt cut. Each saved dump gets a JSON sidecar
                      (window start/end, SCTE-35 cue_seq values seen --
                      joinable with --scte35-out) alongside the .ts file.
                      See --ts-dump-window/--ts-dump-all/--ts-dump-max-files
                      and the "Raw TS dump around SCTE-35 events" README
                      section for the memory/disk trade-offs before
                      enabling this on a high-bitrate feed.
  time_to_event_ms / actual_preroll_ms / preroll_delta_ms / preroll_verdict
                      Always computed (no flag needed), in every match/miss
                      CSV/JSON row and the console verdict line: how much
                      lead time the SCTE-35 cue's own PTS declared
                      (time_to_event_ms), how much lead time was actually
                      observed in real time (actual_preroll_ms), and their
                      difference (preroll_delta_ms). Flagged
                      preroll_verdict=PREROLL_SHORT when the actual pre-roll
                      falls more than --preroll-tolerance-ms short of the
                      declared one -- i.e. downstream ad-decisioning got
                      less real reaction time than the cue implied. See
                      caveat 6 above and the "Time to event vs. actual
                      pre-roll" README section before treating this as a
                      strict SCTE-35 conformance check -- it is not one.
  signal_verdict      Always computed (no flag needed): whether the FIRST
                      registered transmission of a given splice_event_id
                      met SCTE-35's own apparent minimum advance-notice
                      requirement (>= --min-time-to-event-ms, default
                      4000ms -- see caveat 7 above). SIGNAL_LATE if below
                      that floor, OK if not, RETRANSMISSION for later
                      repeats of the same event_id (not re-evaluated), N/A
                      if time_to_event_ms itself could not be computed.
                      Unlike preroll_verdict, this one IS meant to reflect
                      an actual (secondary-source-cited, not
                      primary-verified) SCTE-35 requirement -- see caveat 7.
"""

import argparse
import collections
import csv
import json
import logging
import os
import queue
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

try:
    from threefive3 import Cue
except ImportError:  # fall back to the older package name
    try:
        from threefive import Cue
    except ImportError:
        Cue = None

__version__ = "0.10"

TS_PACKET_SIZE = 188
SYNC_BYTE = 0x47
PTS_HZ = 90000
PTS_MAX = 1 << 33  # 33-bit PTS counter

STREAM_TYPE_MPEG2_VIDEO = 0x02
STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_HEVC = 0x24
STREAM_TYPE_SCTE35 = 0x86

VIDEO_STREAM_TYPES = {
    STREAM_TYPE_MPEG2_VIDEO: "mpeg2",
    STREAM_TYPE_H264: "h264",
    STREAM_TYPE_HEVC: "hevc",
}

log = logging.getLogger("scte35_idr_diff")


# --------------------------------------------------------------------------
# PTS helpers
# --------------------------------------------------------------------------

def pts_diff_seconds(a_ticks, b_ticks):
    """Signed difference (a - b) in seconds, accounting for 33-bit wraparound
    by picking the shorter path around the circle."""
    diff = (a_ticks - b_ticks) % PTS_MAX
    if diff > PTS_MAX // 2:
        diff -= PTS_MAX
    return diff / PTS_HZ


def read_pts_dts(bits5):
    """Decode a 5-byte (40-bit) PTS or DTS field per ISO/IEC 13818-1."""
    b = bits5
    pts = ((b[0] >> 1) & 0x07) << 30
    pts |= b[1] << 22
    pts |= (b[2] >> 1) << 15
    pts |= b[3] << 7
    pts |= b[4] >> 1
    return pts


# --------------------------------------------------------------------------
# PSI (PAT/PMT) parsing -- just enough to locate video + SCTE-35 PIDs
# --------------------------------------------------------------------------

class SectionReassembler:
    """Reassembles PSI sections (PAT/PMT) from TS packets on one PID."""

    def __init__(self):
        self.buf = bytearray()
        self.started = False

    def push(self, payload, pusi):
        if pusi:
            pointer = payload[0]
            self.buf = bytearray(payload[1 + pointer:])
            self.started = True
        elif self.started:
            self.buf += payload
        sections = []
        while self.started and len(self.buf) >= 3:
            section_length = ((self.buf[1] & 0x0F) << 8) | self.buf[2]
            total = 3 + section_length
            if len(self.buf) < total:
                break
            sections.append(bytes(self.buf[:total]))
            self.buf = self.buf[total:]
        return sections


def parse_pat(section):
    """Returns list of (program_number, pmt_pid)."""
    programs = []
    data = section[8:-4]  # skip section header, drop CRC32
    for i in range(0, len(data), 4):
        program_number = (data[i] << 8) | data[i + 1]
        pid = ((data[i + 2] & 0x1F) << 8) | data[i + 3]
        if program_number != 0:  # skip NIT PID entry
            programs.append((program_number, pid))
    return programs


def parse_pmt(section):
    """Returns (pcr_pid, [(stream_type, elementary_pid, descriptors_bytes)])."""
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    pos = 12 + program_info_length
    end = len(section) - 4  # drop CRC32
    streams = []
    while pos < end:
        stream_type = section[pos]
        elementary_pid = ((section[pos + 1] & 0x1F) << 8) | section[pos + 2]
        es_info_length = ((section[pos + 3] & 0x0F) << 8) | section[pos + 4]
        descriptors = section[pos + 5: pos + 5 + es_info_length]
        streams.append((stream_type, elementary_pid, descriptors))
        pos += 5 + es_info_length
    return pcr_pid, streams


def _is_jsonable(value):
    try:
        json.dumps(value)
        return True
    except TypeError:
        return False


def _shallow_obj_to_dict(obj):
    """Best-effort dump of a threefive sub-object's public attributes,
    keeping only JSON-serializable values. Used as a last-resort fallback
    if cue.get()/cue.get_json() are unavailable in a given threefive
    version."""
    if obj is None:
        return None
    try:
        return {k: v for k, v in vars(obj).items()
                if not k.startswith("_") and _is_jsonable(v)}
    except TypeError:
        return str(obj)


def cue_to_dict(cue):
    """Return the FULL decoded SCTE-35 cue as a plain dict: info_section,
    command, and every descriptor (segmentation_descriptor etc.) with all
    their fields -- not just the handful of fields this tool's matching
    logic cares about. Tries threefive's own cue.get() / cue.get_json()
    first (these already return everything threefive parsed), and only
    falls back to a manual attribute dump if those aren't available."""
    try:
        d = cue.get()
        if isinstance(d, dict):
            return d
    except Exception:  # noqa: BLE001
        pass
    try:
        j = cue.get_json()
        if isinstance(j, str):
            return json.loads(j)
        if isinstance(j, dict):
            return j
    except Exception:  # noqa: BLE001
        pass
    return {
        "info_section": _shallow_obj_to_dict(getattr(cue, "info_section", None)),
        "command": _shallow_obj_to_dict(getattr(cue, "command", None)),
        "descriptors": [_shallow_obj_to_dict(d) for d in (getattr(cue, "descriptors", None) or [])],
    }


def summarize_descriptors(cue):
    """Human-readable one-liner per descriptor (mainly segmentation_descriptor,
    which is where SCTE-35 carries the actual event semantics -- CUE-OUT/IN
    type, segmentation_event_id, UPID, duration -- for time_signal() cues,
    since the time_signal command itself carries no event identity)."""
    lines = []
    for d in (getattr(cue, "descriptors", None) or []):
        cls = type(d).__name__
        fields = []
        for name in ("segmentation_event_id", "segmentation_type_id",
                     "segmentation_type_id_name", "segmentation_upid_type",
                     "segmentation_upid", "segment_num", "segments_expected",
                     "segmentation_duration", "duration"):
            val = getattr(d, name, None)
            if val is not None:
                fields.append(f"{name}={val}")
        lines.append(cls + ("(" + ", ".join(fields) + ")" if fields else ""))
    return lines


def has_cuei_registration(descriptors):
    """SCTE-35 ES loops should carry a registration_descriptor (tag 0x05)
    with format_identifier 'CUEI'."""
    pos = 0
    while pos + 2 <= len(descriptors):
        tag = descriptors[pos]
        length = descriptors[pos + 1]
        payload = descriptors[pos + 2: pos + 2 + length]
        if tag == 0x05 and payload[:4] == b"CUEI":
            return True
        pos += 2 + length
    return False


# --------------------------------------------------------------------------
# PES reassembly
# --------------------------------------------------------------------------

class PesReassembler:
    """Reassembles PES packets from TS packets on one PID and yields
    (pts_ticks_or_None, dts_ticks_or_None, elementary_stream_payload)."""

    def __init__(self):
        self.buf = bytearray()
        self.started = False

    def push(self, payload, pusi):
        out = None
        if pusi:
            if self.started and self.buf:
                out = self._parse(self.buf)
            self.buf = bytearray(payload)
            self.started = True
        elif self.started:
            self.buf += payload
        return out

    def flush(self):
        if self.started and self.buf:
            out = self._parse(self.buf)
            self.buf = bytearray()
            return out
        return None

    @staticmethod
    def _parse(buf):
        if len(buf) < 9 or buf[0:3] != b"\x00\x00\x01":
            return None
        flags = buf[7]
        pts_dts_flags = (flags >> 6) & 0x03
        header_data_length = buf[8]
        header_end = 9 + header_data_length
        if header_end > len(buf):
            return None
        pts = dts = None
        off = 9
        if pts_dts_flags in (0x02, 0x03):
            pts = read_pts_dts(buf[off:off + 5])
            off += 5
        if pts_dts_flags == 0x03:
            dts = read_pts_dts(buf[off:off + 5])
            off += 5
        es_payload = bytes(buf[header_end:])
        return pts, dts, es_payload


# --------------------------------------------------------------------------
# NAL unit scanning (Annex B) -- H.264 / HEVC IDR detection
# --------------------------------------------------------------------------

def iter_nal_units(data):
    """Yield (nal_type_or_None, start_offset, end_offset) for every Annex B
    NAL unit in data, where data[start_offset:end_offset] is the COMPLETE
    NAL unit including its own start code prefix (3 or 4 bytes). nal_type
    is None here (codec-agnostic offsets only); callers derive the type
    from the header byte per codec, since H.264/HEVC mask it differently."""
    n = len(data)
    start_codes = []  # offsets of the 00 00 01 (3-byte) marker itself
    i = 0
    while i < n - 2:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            start_codes.append(i)
            i += 3
        else:
            i += 1
    units = []
    for idx, sc in enumerate(start_codes):
        unit_start = sc - 1 if sc > 0 and data[sc - 1] == 0 else sc
        header_off = sc + 3
        unit_end = start_codes[idx + 1] - 1 if idx + 1 < len(start_codes) else n
        # unit_end above assumes the next unit's start code has no leading
        # zero byte belonging to this unit's trailing padding; trim any
        # leftover trailing zero bytes is unnecessary for our purposes
        # (type/parameter-set extraction only reads the header + payload).
        if idx + 1 < len(start_codes):
            next_sc = start_codes[idx + 1]
            next_unit_start = next_sc - 1 if next_sc > 0 and data[next_sc - 1] == 0 else next_sc
            unit_end = next_unit_start
        if header_off >= n:
            continue
        units.append((header_off, unit_start, unit_end))
    return units


def find_nal_types(es_payload, codec):
    """Yield nal_unit_type for every NAL unit found via Annex B start codes."""
    for header_off, _unit_start, _unit_end in iter_nal_units(es_payload):
        b0 = es_payload[header_off]
        if codec == "h264":
            yield b0 & 0x1F
        elif codec == "hevc":
            yield (b0 >> 1) & 0x3F


def extract_param_set_nals(es_payload, codec):
    """Return the raw bytes (with start codes) of every SPS/PPS (H.264) or
    VPS/SPS/PPS (HEVC) NAL unit found in this access unit, concatenated in
    order. Used to build a cache of the most recently seen parameter sets,
    so a lone IDR access unit can still be decoded standalone by ffmpeg
    even on a stream where repeatHeaders=0 (parameter sets sent only once,
    not repeated before every IDR)."""
    out = bytearray()
    for header_off, unit_start, unit_end in iter_nal_units(es_payload):
        b0 = es_payload[header_off]
        if codec == "h264":
            nal_type = b0 & 0x1F
            is_param_set = nal_type in (7, 8)  # SPS, PPS
        elif codec == "hevc":
            nal_type = (b0 >> 1) & 0x3F
            is_param_set = nal_type in (32, 33, 34)  # VPS, SPS, PPS
        else:
            is_param_set = False
        if is_param_set:
            out += es_payload[unit_start:unit_end]
    return bytes(out)


def has_param_sets(es_payload, codec):
    """True if this access unit already carries its own SPS/PPS (H.264) or
    VPS/SPS/PPS (HEVC) -- e.g. repeatHeaders=1 on the encoder -- meaning we
    should NOT prepend cached parameter sets before feeding it to ffmpeg
    (that would duplicate them)."""
    return len(extract_param_set_nals(es_payload, codec)) > 0


def classify_idr(nal_types, codec, include_cra):
    """Returns 'idr', 'cra', or None for a set of NAL types found in one AU."""
    types = set(nal_types)
    if codec == "h264":
        return "idr" if 5 in types else None
    if codec == "hevc":
        if types & {19, 20}:
            return "idr"
        if include_cra and 21 in types:
            return "cra"
        return None
    if codec == "mpeg2":
        # Not handled at NAL granularity; MPEG-2 uses picture_coding_type
        # in the picture header. Out of scope for this tool -- flag it.
        return None
    return None


# --------------------------------------------------------------------------
# Misc CLI helpers
# --------------------------------------------------------------------------

def parse_duration_seconds(value):
    """Parse a human duration like '30', '30s', '90s', '2m', '1.5m', '1h'
    into seconds (float). A bare number is seconds. Used for
    --ts-dump-window so the window can be given in whichever unit is
    convenient (seconds or minutes)."""
    s = str(value).strip().lower()
    # Longest suffix first so "ms" is checked before the bare "s" suffix
    # (which "ms" would also match).
    for suffix, multiplier in (("ms", 0.001), ("s", 1.0), ("m", 60.0), ("h", 3600.0)):
        if s.endswith(suffix):
            number_part = s[: -len(suffix)]
            try:
                return float(number_part) * multiplier
            except ValueError:
                raise argparse.ArgumentTypeError(f"invalid duration: {value!r}")
    try:
        return float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid duration: {value!r}")


def validate_ipv4_literal(value, flag_name):
    """Raise a clear, actionable error if `value` is not a plain dotted-
    quad IPv4 address (e.g. 239.1.1.1) -- used for --addr/--iface.

    Why this exists: socket.socket.bind((host, port)) resolves `host`
    through the system's normal getaddrinfo()/NSS machinery even when it's
    already a numeric literal, same as it would for a DNS hostname. If
    `value` is anything other than a valid IPv4 literal (a typo, a stray
    character, or an actual hostname that isn't resolvable on THIS host --
    e.g. a headend/internal DNS name that only exists on the server you
    tested on before), bind() fails with the opaque
    "socket.gaierror: [Errno -2] Name or service not known" and a raw
    traceback instead of a message that says what's actually wrong.
    socket.inet_aton() below does pure string parsing -- it never touches
    DNS/NSS -- so this check fails fast with a clear diagnosis before any
    network call is attempted."""
    try:
        socket.inet_aton(value)
    except OSError:
        log.error(
            "%s must be a plain numeric IPv4 address (e.g. 239.1.1.1), not a hostname -- "
            "got %r. This is also the most common cause of the confusing "
            "'socket.gaierror: Name or service not known' error: a DNS name (or internal "
            "hosts-file entry) that resolved on one server may simply not exist on another.",
            flag_name, value)
        sys.exit(1)


def is_multicast_ipv4(addr):
    """True if `addr` (a dotted-quad string that has already passed
    validate_ipv4_literal()) falls in the IPv4 multicast range
    224.0.0.0-239.255.255.255 (i.e. first octet 224-239). Used by
    open_multicast_socket() to decide whether to actually join a multicast
    group (IP_ADD_MEMBERSHIP) or just listen for plain unicast UDP --
    joining a "multicast group" that isn't actually a multicast address
    (e.g. 127.0.0.1, or any regular unicast IP) fails at the socket layer
    with an opaque OSError, not a useful error message."""
    first_octet = int(addr.split(".")[0])
    return 224 <= first_octet <= 239


# --------------------------------------------------------------------------
# Multicast / RTP ingest
# --------------------------------------------------------------------------

def open_multicast_socket(addr, port, iface):
    # Validate BEFORE any network call: inet_aton() is pure string parsing
    # (never touches DNS/NSS), so this fails fast with a clear message
    # instead of bind() below raising an opaque socket.gaierror -- see
    # validate_ipv4_literal() for why that matters on a host where addr
    # isn't a valid literal (typo, or a hostname not resolvable here).
    validate_ipv4_literal(addr, "--addr")
    if iface:
        validate_ipv4_literal(iface, "--iface")

    # --addr is only actually joined as a multicast group if it's IN the
    # IPv4 multicast range (224.0.0.0-239.255.255.255). Any other value --
    # most usefully 127.0.0.1 for local testing (e.g. against ffmpeg/tsp
    # sending plain UDP straight to loopback, no real network or IGMP
    # required), but this also covers a host's own unicast interface
    # address -- is instead just bound and listened on directly as plain
    # unicast UDP, with no IP_ADD_MEMBERSHIP join at all. Attempting that
    # join against a non-multicast address fails at the socket layer with
    # an opaque "OSError: [Errno 22] Invalid argument", not a useful
    # message, so this is decided up front instead of being left to blow up
    # there.
    multicast = is_multicast_ipv4(addr)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass  # not available on every platform; SO_REUSEADDR above is enough on Linux
    # Bind to the SPECIFIC multicast group address, not "" (INADDR_ANY).
    # A socket bound to INADDR_ANY:port is only filtered by destination
    # PORT, not by multicast group -- if another process on this host has
    # joined a *different* group on the same port (extremely common in a
    # headend where many SPTS channels share one UDP port across different
    # multicast addresses), the kernel delivers BOTH groups' traffic to
    # BOTH sockets once either one has caused that port to receive
    # multicast at all. Binding to the group address instead of "" makes
    # the kernel filter by destination address too, so running several
    # instances of this tool against different multicast groups (even on
    # the same port) no longer cross-contaminates each other's PAT/PMT/
    # SCTE-35/video parsing.
    try:
        sock.bind((addr, port))
    except socket.gaierror as exc:
        # Defense in depth: validate_ipv4_literal() above should already
        # have caught a non-numeric addr, so reaching this normally means
        # something more unusual about the host's resolver setup. Fail with
        # a diagnosis instead of a bare traceback either way.
        log.error(
            "Failed to bind to %s:%d (%s). %r passed IPv4-literal validation but the "
            "system's own address resolution (getaddrinfo) still rejected it -- this can "
            "happen on a host with a broken/missing resolver configuration "
            "(e.g. no /etc/nsswitch.conf or /etc/resolv.conf). Try binding to 0.0.0.0 "
            "manually to confirm, or check the host's network/DNS configuration.",
            addr, port, exc, addr)
        sock.close()
        sys.exit(1)

    if multicast:
        mreq = struct.pack("4s4s", socket.inet_aton(addr),
                            socket.inet_aton(iface) if iface else socket.INADDR_ANY.to_bytes(4, "big"))
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as exc:
            # Defense in depth, mirroring the bind() handling above: this
            # normally shouldn't happen (addr is confirmed multicast at
            # this point), but an unusual host network stack/NIC could
            # still reject the join, and a raw traceback here isn't
            # actionable.
            log.error(
                "Failed to join multicast group %s on port %d (%s). Check that %s is "
                "actually reachable/routed on this host's network (or the interface given "
                "via --iface, currently %s), and that no firewall/iptables rule is blocking "
                "IGMP.", addr, port, exc, addr, iface or "default route")
            sock.close()
            sys.exit(1)
    else:
        if iface:
            log.warning(
                "--iface (%s) is ignored: %s is not a multicast address, so there is no "
                "multicast group to join on a specific interface.", iface, addr)
        log.warning(
            "%s is not a multicast address (224.0.0.0-239.255.255.255) -- running in "
            "UNICAST/local-test mode instead: listening for plain UDP sent directly to "
            "%s:%d, with no multicast group join. Useful for local testing (e.g. "
            "--addr 127.0.0.1 against an ffmpeg/tsp test stream aimed at loopback), but "
            "double-check this is intentional -- a mistyped multicast address would also "
            "silently land here instead of erroring.", addr, addr, port)

    sock.settimeout(2.0)
    return sock


def looks_like_rtp(payload):
    if len(payload) < 12:
        return False
    version = (payload[0] >> 6) & 0x03
    payload_type = payload[1] & 0x7F
    # RTP v2 carrying MPEG-TS is payload type 33 (MP2T) by convention, but
    # some vendors use dynamic PTs -- version==2 plus a TS sync byte right
    # after a plausible 12-byte header is a decent heuristic fallback.
    if version == 2 and payload_type == 33:
        return True
    if version == 2 and len(payload) > 12 and payload[12] == SYNC_BYTE:
        return True
    return False


def strip_rtp(payload):
    if len(payload) < 12:
        return payload
    csrc_count = payload[0] & 0x0F
    header_len = 12 + 4 * csrc_count
    ext = (payload[0] >> 4) & 0x01
    if ext and len(payload) >= header_len + 4:
        ext_len_words = struct.unpack(">H", payload[header_len + 2:header_len + 4])[0]
        header_len += 4 + 4 * ext_len_words
    return payload[header_len:]


# --------------------------------------------------------------------------
# Detailed video stream info (scte35-analyzer addition) -- ffprobe helpers
# --------------------------------------------------------------------------
# Module-level, not methods on Probe, since neither function touches any
# Probe state -- both just take a path to a short local TS sample (built by
# Probe._sample_video_info from its raw-packet ring buffer) and return
# plain dicts. Kept in this file rather than a separate module so the CLI
# tool this file is forked from stays a single, dependency-free script (see
# the module docstring); ffprobe itself is already a hard dependency of the
# GUI project (see app/media/segments.py), just not of the bare CLI tool --
# these two functions are simply never called unless a GUI job turns
# video_info_enabled on.

def _rate_to_fps(rate_str):
    """ffprobe's r_frame_rate/avg_frame_rate are "num/den" strings (e.g.
    "30000/1001" for 29.97) -- convert to a plain float for display, or
    None if missing/unparseable."""
    if not rate_str or "/" not in rate_str:
        return None
    num_s, _, den_s = rate_str.partition("/")
    try:
        num, den = float(num_s), float(den_s)
    except ValueError:
        return None
    return round(num / den, 3) if den else None


def _scan_type_from_field_order(field_order):
    """ffprobe's field_order is one of "progressive", "tt"/"bb"/"tb"/"bt"
    (top/bottom field first, interlaced), or "unknown". Collapse that down
    to the plain progressive/interlaced/unknown distinction the GUI shows."""
    if not field_order or field_order == "unknown":
        return "unknown"
    return "progressive" if field_order == "progressive" else "interlaced"


def _ffprobe_gop_structure(ts_path, timeout, max_frames=300):
    """Reads up to max_frames decoded picture types from the video stream
    and derives (a) the average distance between key frames (IDR/I, the
    GOP length) and (b) a short pattern string (e.g. "IBBPBBPBBPBB") for
    the first full cycle actually seen. Best-effort: returns None on any
    ffprobe failure, and a short/low-framerate sample may not contain two
    full GOPs, in which case avg_gop_length_frames is null but whatever
    prefix was captured is still returned in `pattern` so there's still
    something to show. -read_intervals caps how much of the sample ffprobe
    actually has to decode, since this (unlike the plain -show_streams
    call in _ffprobe_video_info) has to walk real frame data, not just
    container/SPS metadata."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "frame=pict_type,key_frame",
        "-read_intervals", f"%+#{max_frames}",
        "-of", "json", ts_path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        frames = json.loads(proc.stdout.decode(errors="replace") or "{}").get("frames") or []
    except json.JSONDecodeError:
        return None
    if not frames:
        return None
    types = [f.get("pict_type") or "?" for f in frames]
    key_positions = [i for i, f in enumerate(frames) if f.get("key_frame") == 1]
    spacings = [b - a for a, b in zip(key_positions, key_positions[1:])]
    avg_gop = round(sum(spacings) / len(spacings), 1) if spacings else None
    pattern = ("".join(types[key_positions[0]:key_positions[1]])
               if len(key_positions) >= 2 else "".join(types[:48]))
    return {
        "frames_sampled": len(frames),
        "keyframes_sampled": len(key_positions),
        "avg_gop_length_frames": avg_gop,
        "pattern": pattern,
    }


def _ffprobe_video_info(ts_path, timeout=10):
    """Best-effort: runs ffprobe against a short local TS sample file and
    returns a dict of stream-level technical detail -- pixel format, color
    space, frame rate, field order/scan type, profile/level -- plus a
    nested GOP-structure dict from _ffprobe_gop_structure, or None if
    ffprobe itself failed or found no video stream at all."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,profile,level,width,height,pix_fmt,"
        "color_range,color_space,color_transfer,color_primaries,"
        "field_order,r_frame_rate,avg_frame_rate",
        "-of", "json", ts_path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        streams = json.loads(proc.stdout.decode(errors="replace") or "{}").get("streams") or []
    except json.JSONDecodeError:
        return None
    if not streams:
        return None
    s = streams[0]  # -select_streams v:0 -- at most one stream can match
    return {
        "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "codec_name": s.get("codec_name"),
        "profile": s.get("profile"),
        "level": s.get("level"),
        "width": s.get("width"),
        "height": s.get("height"),
        "pix_fmt": s.get("pix_fmt"),
        "color_range": s.get("color_range"),
        "color_space": s.get("color_space"),
        "color_transfer": s.get("color_transfer"),
        "color_primaries": s.get("color_primaries"),
        "field_order": s.get("field_order"),
        "scan_type": _scan_type_from_field_order(s.get("field_order")),
        "r_frame_rate": s.get("r_frame_rate"),
        "avg_frame_rate": s.get("avg_frame_rate"),
        "frame_rate_fps": _rate_to_fps(s.get("r_frame_rate")),
        "gop": _ffprobe_gop_structure(ts_path, timeout=timeout),
    }


# --------------------------------------------------------------------------
# Main probe
# --------------------------------------------------------------------------

class Probe:
    def __init__(self, args, on_event=None):
        # `on_event(kind, data)` is an optional hook, added for the
        # scte35-analyzer project: a live consumer (the GUI backend's job
        # manager) can pass a callback here to receive the exact same
        # structured records this probe already produces for
        # --csv-out/--json-out/--scte35-out/snapshots/ts-dumps, as they
        # happen, without needing to tail files. `args` is intentionally
        # still just "anything with the right attributes" (an argparse
        # Namespace from the CLI, or a plain SimpleNamespace built by the
        # GUI backend) -- nothing below needs to change for that.
        self._on_event = on_event
        self.args = args
        self.pat_reasm = SectionReassembler()
        self.pmt_reasm = None
        self.pmt_pid = None
        self.video_pid = args.pid_video
        self.scte35_pid = args.pid_scte35
        self.video_codec = args.codec
        self.pes_video = PesReassembler()
        self.pes_scte35 = PesReassembler()  # SCTE-35 PID often section-based, handled separately
        self.scte35_section = SectionReassembler()
        self.pending = []  # list of dicts: target_pts, event_id, kind, deadline_wallclock
        self.match_window_s = args.tolerance_ms / 1000.0
        self.max_early_s = args.max_early_ms / 1000.0
        self.timeout_s = args.timeout_s
        self.ok_threshold_ms = args.ok_threshold_ms
        self.preroll_tolerance_ms = getattr(args, "preroll_tolerance_ms", 500.0)
        # SCTE-35's own minimum advance-notice requirement (per secondary
        # sources citing ANSI/SCTE 35 (2019) 9.2/10.3.3: "sent at least once
        # a minimum of 4 seconds in advance of the desired splice time") --
        # see _register_cue()/signal_verdict and the "Time to event vs.
        # actual pre-roll" README section for the caveats on this citation
        # and on what "first signal" means for a retransmitted event.
        self.min_time_to_event_ms = getattr(args, "min_time_to_event_ms", 4000.0)
        # splice_event_id values already seen, so a legitimately retransmitted
        # copy of the same event (common practice, to survive packet loss) is
        # not re-evaluated against the 4-second-minimum requirement -- that
        # requirement is about the FIRST time the event is signaled, not
        # every repeat as the target PTS gets closer (which would trivially
        # -- and wrongly -- flag every retransmitted event as late).
        self._seen_scte35_event_ids = set()
        self.include_cra = args.include_cra
        # Most recently observed video access unit's PTS, updated on EVERY
        # AU (not just IDRs) -- used as the "live video position" reference
        # point for computing each cue's declared time-to-event at the
        # moment it's registered. See _register_cue()/_emit() for the
        # actual/declared pre-roll measurement this feeds.
        self.last_video_pts_ticks = None
        self.scte35_seq = 0
        self.csv_writer = None
        self.csv_file = None
        self.json_out = None
        self.scte35_out = None
        self.scte35_logger = log
        self.last_param_sets = b""  # most recently seen SPS/PPS(/VPS) NALs
        self.snapshot_dir = args.snapshot_dir
        self.snapshot_all_idr = args.snapshot_all_idr
        self.pre_frames = max(0, getattr(args, "pre_frames", 0) or 0)
        # -- Reference-frame snapshots (scte35-analyzer addition) -----------------
        # Two independent, user-selectable "which exact frame do these
        # numbers point to?" captures, each off by default:
        #   time_to_event_snapshot: "off" | "cue_arrival_frame" | "target_pts_frame"
        #     - cue_arrival_frame: the frame that was on air at the moment
        #       the SCTE-35 cue was registered (self.last_video_pts_ticks at
        #       that instant) -- "what was playing when the cue arrived".
        #     - target_pts_frame: the frame at the literal PTS the cue's
        #       target_ticks points to -- captured once a video AU with that
        #       PTS (or the first one at/after it) is actually seen.
        #   preroll_snapshot: "off" | "realtime_deadline_frame" | "same_as_matched_idr"
        #     - realtime_deadline_frame: whatever frame is on air at the
        #       real wall-clock moment the cue's declared time_to_event_ms
        #       elapses (a threading.Timer fired at registration time).
        #     - same_as_matched_idr: just the already-captured matched-IDR
        #       snapshot, relabeled -- no extra decode work.
        self.time_to_event_snapshot = getattr(args, "time_to_event_snapshot", "off") or "off"
        self.preroll_snapshot = getattr(args, "preroll_snapshot", "off") or "off"
        # Independent from self.snapshot_dir below: snapshot_dir now also
        # gets turned on purely to serve the reference-snapshot modes above
        # even when the plain "snapshot the matched IDR" feature itself is
        # off (see scte35-analyzer's build_probe_args), so the matched-IDR
        # capture in _process_video_pes needs its own gate to stay off in
        # that case. Defaults True so the original CLI tool (which has no
        # such concept -- --snapshot-dir alone always meant "capture
        # matched IDRs") is unaffected.
        self.snapshot_matched_idr_enabled = bool(getattr(args, "snapshot_enabled", True))
        # threading.Timer objects for pending realtime_deadline_frame
        # captures, kept only so close() can cancel any still outstanding.
        self._reference_snapshot_timers = []
        needs_au_buffer = (
            self.pre_frames > 0
            or self.time_to_event_snapshot != "off"
            or self.preroll_snapshot == "realtime_deadline_frame"
        )
        au_buffer_size = getattr(args, "au_buffer_size", None) or max(300, self.pre_frames * 15)
        # Rolling buffer of EVERY access unit (not just IDRs), in transport/
        # decode order, so a snapshot request for "N frames before this IDR"
        # (or for an arbitrary reference frame, see _enqueue_arbitrary_frame_
        # snapshot below) can walk back to the previous IDR/CRA (a guaranteed
        # decodable GOP boundary) and hand ffmpeg a complete chunk. Only
        # allocated when something actually needs it.
        self.au_buffer = collections.deque(maxlen=au_buffer_size) if needs_au_buffer else None
        # idr_ticks -> [pre-frame snapshot paths, oldest first], populated by
        # _enqueue_snapshot_sequence and consumed by _emit(). Bounded so a
        # long-running probe with --snapshot-all-idr (which enqueues
        # sequences for IDRs that never end up matching a pending cue, and
        # so are never popped) can't grow this unboundedly.
        self._pending_pre_frame_paths = collections.OrderedDict()
        self.snapshot_queue = None
        self.snapshot_thread = None
        if self.snapshot_dir:
            os.makedirs(self.snapshot_dir, exist_ok=True)
            self.snapshot_queue = queue.Queue()
            self.snapshot_thread = threading.Thread(
                target=self._snapshot_worker, daemon=True)
            self.snapshot_thread.start()

        # -- Raw TS dump around SCTE-35 events --------------------------
        self.ts_dump_dir = getattr(args, "ts_dump_dir", None)
        self.ts_dump_window_s = getattr(args, "ts_dump_window", None)
        self.ts_dump_all = bool(getattr(args, "ts_dump_all", False))
        self.ts_dump_include_previous = not getattr(args, "ts_dump_no_preroll", False)
        self.ts_dump_max_files = getattr(args, "ts_dump_max_files", None)
        self.ts_dump_queue = None
        self.ts_dump_thread = None
        self._ts_dump_cur = None
        self._ts_dump_prev = b""
        self._ts_dump_window_start = None
        self._ts_dump_window_start_wall = None
        self._ts_dump_event_count = 0
        self._ts_dump_event_seqs = []
        self._ts_dump_seq = 0  # monotonic counter, disambiguates filenames within one wallclock second
        if self.ts_dump_dir:
            os.makedirs(self.ts_dump_dir, exist_ok=True)
            self._ts_dump_cur = bytearray()
            self._ts_dump_window_start = time.monotonic()
            self._ts_dump_window_start_wall = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            self.ts_dump_queue = queue.Queue()
            self.ts_dump_thread = threading.Thread(
                target=self._ts_dump_worker, daemon=True)
            self.ts_dump_thread.start()
            log.info(
                "TS dump enabled: window=%.1fs dir=%s only-on-event=%s "
                "include-previous-window=%s max-files=%s",
                self.ts_dump_window_s, self.ts_dump_dir, not self.ts_dump_all,
                self.ts_dump_include_previous, self.ts_dump_max_files or "unlimited")

        # -- Detailed video stream info (scte35-analyzer addition) --------
        # Periodically samples a short window of raw TS packets and shells
        # out to ffprobe for stream-level technical detail beyond what this
        # probe's own PES/NAL parsing already extracts for IDR matching:
        # pixel format, color space, frame rate, field order (progressive
        # vs. interlaced), profile/level, and an approximate GOP structure.
        # Deliberately NOT hand-parsed from SPS/VUI here: ffprobe already
        # does this reliably across H.264/HEVC/MPEG-2 and is already a hard
        # dependency of this project (see app/media/segments.py's playback
        # remux), so this reuses it instead of duplicating exp-golomb
        # bitstream parsing. Only meaningful with a live consumer -- with no
        # on_event hook there is nowhere for the result to go, so it's not
        # even started in that case (e.g. the plain CLI tool).
        self.video_info_enabled = (
            bool(getattr(args, "video_info_enabled", False)) and self._on_event is not None)
        self.video_info_interval_s = float(getattr(args, "video_info_interval_s", None) or 20.0)
        # Updated by _sample_video_info() whenever it gets a usable
        # frame_rate_fps + gop.avg_gop_length_frames pair -- read by _emit()
        # to compute gop_verdict (see its own comment there). None until the
        # first successful sample lands (or forever, if video_info is
        # disabled/never manages a clean ffprobe read), in which case
        # gop_verdict is reported as N/A rather than guessed at.
        self._last_gop_duration_ms = None
        self._video_info_buffer = None
        self.video_info_thread = None
        self._video_info_stop = None
        if self.video_info_enabled:
            # ~4.5 MB (188 bytes * 24000) of the most recent packets, ALL
            # PIDs (like the TS-dump buffer above) so ffprobe can see
            # PAT/PMT plus several GOPs of the video stream regardless of
            # bitrate, without holding more than that in memory. A plain
            # deque, not a queue: the worker below reads a snapshot of it
            # directly rather than draining it -- this is periodic sampling
            # of "the stream right now", not a log of discrete events.
            self._video_info_buffer = collections.deque(maxlen=24000)
            self._video_info_stop = threading.Event()
            self.video_info_thread = threading.Thread(
                target=self._video_info_worker, daemon=True)
            self.video_info_thread.start()

        if args.csv_out:
            self.csv_file = open(args.csv_out, "a", newline="")
            self.csv_writer = csv.writer(self.csv_file)
            if self.csv_file.tell() == 0:
                self.csv_writer.writerow([
                    # NOTE: if you already have an older CSV from before this
                    # column set, point --csv-out at a NEW file -- the header
                    # is only (re)written when the file is empty, so an old
                    # file would silently get misaligned columns appended.
                    "wallclock", "cue_seq", "event_id", "command_type", "out_of_network",
                    "target_pts_s", "raw_pts_time_s", "pts_adjustment_ticks",
                    "idr_pts_s", "delta_ms", "verdict", "codec", "au_kind",
                    "segmentation_summary", "snapshot_path", "pre_frame_snapshot_paths",
                    "time_to_event_ms", "actual_preroll_ms", "preroll_delta_ms", "preroll_verdict",
                    "signal_verdict", "gop_verdict", "near_miss_ms",
                ])
        if args.json_out:
            self.json_out = open(args.json_out, "a")
        if args.scte35_out:
            self.scte35_out = open(args.scte35_out, "a")
        if args.scte35_out or args.scte35_log_file:
            # Separate logger -> optionally a separate handler/file,
            # independent of the main console log and of --csv-out/--json-out
            # (which only record match/miss *outcomes*, not the raw SCTE-35
            # content). Set an explicit level so this works regardless of
            # whatever level the root logger ends up at.
            self.scte35_logger = logging.getLogger("scte35_idr_diff.scte35")
            self.scte35_logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
            self.scte35_logger.propagate = True  # still show a summary on console too
            if args.scte35_log_file:
                handler = logging.FileHandler(args.scte35_log_file)
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                handler.setLevel(logging.DEBUG if args.verbose else logging.INFO)
                self.scte35_logger.addHandler(handler)
                self.scte35_logger.propagate = False  # avoid double-printing to console

    # -- Live event hook (scte35-analyzer addition) --------------------------

    def _emit_event(self, kind, data):
        """Best-effort hook for a live consumer (the GUI backend's job
        manager) -- must never be able to crash the probe itself, since
        the socket-receive loop / file reader depends on this object
        staying alive regardless of what a GUI-side bug does."""
        if self._on_event is None:
            return
        try:
            self._on_event(kind, data)
        except Exception:  # noqa: BLE001
            log.exception("on_event callback failed for kind=%s", kind)

    # -- PSI handling --------------------------------------------------

    def handle_pat(self, payload, pusi):
        for section in self.pat_reasm.push(payload, pusi):
            programs = parse_pat(section)
            if not programs:
                continue
            program_number, pmt_pid = programs[0]
            if self.args.program and self.args.program != program_number:
                for pn, pid_ in programs:
                    if pn == self.args.program:
                        pmt_pid = pid_
                        break
            if pmt_pid != self.pmt_pid:
                log.info("PAT: program %d -> PMT PID 0x%X", program_number, pmt_pid)
                self.pmt_pid = pmt_pid
                self.pmt_reasm = SectionReassembler()
                self._emit_event("pat_info", {"program_number": program_number, "pmt_pid": pmt_pid})

    def handle_pmt(self, payload, pusi):
        for section in self.pmt_reasm.push(payload, pusi):
            pcr_pid, streams = parse_pmt(section)
            changed = False
            for stream_type, pid_, descriptors in streams:
                if stream_type in VIDEO_STREAM_TYPES:
                    # Auto-detect the video PID if none was given on the
                    # command line; either way, fill in the codec from the
                    # PMT stream_type if --codec was not forced explicitly.
                    if self.video_pid is None:
                        self.video_pid = pid_
                        log.info("PMT: video PID 0x%X (%s)", pid_, VIDEO_STREAM_TYPES[stream_type])
                        changed = True
                    if pid_ == self.video_pid and self.video_codec is None:
                        self.video_codec = VIDEO_STREAM_TYPES[stream_type]
                        log.info("PMT: codec for video PID 0x%X is %s", pid_, self.video_codec)
                        changed = True
                if stream_type == STREAM_TYPE_SCTE35 and self.scte35_pid is None:
                    self.scte35_pid = pid_
                    cuei = has_cuei_registration(descriptors)
                    log.info("PMT: SCTE-35 PID 0x%X (CUEI registration descriptor %s)",
                             pid_, "present" if cuei else "absent -- accepted on stream_type 0x86 alone")
                    changed = True
            if changed:
                self._emit_event("pid_info", {
                    "pmt_pid": self.pmt_pid,
                    "video_pid": self.video_pid,
                    "video_codec": self.video_codec,
                    "scte35_pid": self.scte35_pid,
                })

    # -- SCTE-35 handling ------------------------------------------------

    def handle_scte35(self, payload, pusi):
        for section in self.scte35_section.push(payload, pusi):
            if Cue is None:
                log.error("threefive3 is not installed; cannot decode SCTE-35. "
                          "pip install threefive3 --break-system-packages")
                continue
            try:
                cue = Cue(section)
                cue.decode()
            except Exception as exc:  # noqa: BLE001 - keep the probe alive
                log.warning("Failed to decode SCTE-35 section: %s", exc)
                continue
            self.scte35_seq += 1
            seq = self.scte35_seq
            self._ts_dump_note_event(seq)
            self._log_full_cue(seq, cue, section)
            self._register_cue(cue, seq)

    def _log_full_cue(self, seq, cue, section_bytes):
        """Unconditionally record EVERY decoded SCTE-35 message -- including
        splice_null, canceled events, and immediate splices that
        _register_cue() below has nothing to match and will skip -- to the
        dedicated SCTE-35 log/file. This is independent of the match/miss
        CSV or JSON output, which only records outcomes for commands that
        carry a time-specified pts_time."""
        cmd = getattr(cue, "command", None)
        command_type = type(cmd).__name__ if cmd is not None else "Unknown"
        descriptor_summary = summarize_descriptors(cue)
        self.scte35_logger.info(
            "SCTE-35 #%d type=%s descriptors=[%s] section=%d bytes",
            seq, command_type, "; ".join(descriptor_summary) or "none", len(section_bytes),
        )
        # Built unconditionally (not just when --scte35-out is set) so the
        # scte35-analyzer live event hook always gets the full decoded cue, even
        # when the CLI-style file sinks aren't configured.
        full = cue_to_dict(cue)
        record = {
            "tool_version": __version__,
            "cue_seq": seq,
            "wallclock": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "scte35_pid": self.scte35_pid,
            "command_type": command_type,
            "descriptor_summary": descriptor_summary,
            "section_hex": section_bytes.hex(),
            "cue": full,
        }
        self._emit_event("scte35_cue", record)
        if self.scte35_out:
            self.scte35_out.write(json.dumps(record, default=str) + "\n")
            self.scte35_out.flush()

    def _register_cue(self, cue, seq):
        info = getattr(cue, "info_section", None)
        cmd = getattr(cue, "command", None)
        if cmd is None:
            return
        pts_time = getattr(cmd, "pts_time", None)
        if pts_time is None:
            self.scte35_logger.debug(
                "SCTE-35 #%d has no time-specified pts_time (immediate splice, "
                "splice_null, or canceled event) -- nothing to match against IDR.", seq)
            return
        pts_adjustment_s = getattr(info, "pts_adjustment", 0) or 0
        # threefive3's SpliceInfoSection.pts_adjustment is already converted
        # to SECONDS by the library (it divides the raw 33-bit 90kHz field
        # by 90000 internally) -- NOT raw ticks, despite this project's own
        # "_ticks" field name further down. Convert back to ticks explicitly
        # so both the PTS-domain arithmetic below and the stored/displayed
        # value are correct. See the module docstring, item 1, for details;
        # this was verified against threefive3's actual source after a
        # customer report of an unexpectedly-tiny correction being applied.
        pts_adjustment_ticks = int(round(pts_adjustment_s * PTS_HZ))
        target_ticks = (int(round(pts_time * PTS_HZ)) + pts_adjustment_ticks) % PTS_MAX
        event_id = getattr(cmd, "splice_event_id", None)
        out_of_network = getattr(cmd, "out_of_network_indicator", None)
        command_type = type(cmd).__name__
        segmentation_summary = summarize_descriptors(cue)
        now = time.monotonic()
        # "Declared" time-to-event: how far in the future the splice is
        # signaled to occur, measured in the PTS domain, relative to the
        # most recently observed video PTS at the moment THIS cue arrives
        # (i.e. the live video position the probe has actually seen so
        # far -- an approximation of "the PTS at which the SCTE-35 message
        # itself was transmitted", the same concept professional TS
        # analyzers derive via PCR/PTS correlation, here approximated by
        # nearest-preceding video AU instead of a full PCR clock recovery).
        # None if no video AU has been seen yet when the cue arrives.
        if self.last_video_pts_ticks is not None:
            time_to_event_ms = pts_diff_seconds(target_ticks, self.last_video_pts_ticks) * 1000.0
        else:
            time_to_event_ms = None
            self.scte35_logger.debug(
                "SCTE-35 #%d: no video PTS observed yet -- cannot compute a "
                "declared time-to-event for this cue.", seq)

        # -- signal_verdict: was THIS event first signaled far enough ahead
        # of its own splice time to meet SCTE-35's own minimum advance-
        # notice requirement (per secondary sources citing ANSI/SCTE 35
        # (2019) 9.2/10.3.3: "sent at least once a minimum of 4 seconds in
        # advance of the desired splice time", default --min-time-to-event-ms
        # 4000)? That requirement is about the FIRST transmission of a given
        # splice_event_id -- an encoder is expected to retransmit the same
        # event as insurance against packet loss, and each later retransmit
        # necessarily has a smaller time_to_event_ms as the splice point
        # approaches, so only the first occurrence of an event_id is
        # evaluated; later ones are reported as RETRANSMISSION rather than
        # re-flagged as late. If event_id itself is missing/None (nothing to
        # dedupe on), every occurrence is treated as first.
        if event_id is not None and event_id in self._seen_scte35_event_ids:
            signal_verdict = "RETRANSMISSION"
        else:
            if event_id is not None:
                self._seen_scte35_event_ids.add(event_id)
            if time_to_event_ms is None:
                signal_verdict = "N/A"
            elif time_to_event_ms < self.min_time_to_event_ms:
                signal_verdict = "SIGNAL_LATE"
            else:
                signal_verdict = "OK"

        entry = {
            "cue_seq": seq,
            "target_ticks": target_ticks,
            "raw_pts_time": pts_time,
            "pts_adjustment": pts_adjustment_ticks,
            "event_id": event_id,
            "command_type": command_type,
            "out_of_network": out_of_network,
            "segmentation_summary": segmentation_summary,
            "deadline": now + self.timeout_s,
            "register_monotonic": now,
            "time_to_event_ms": time_to_event_ms,
            "signal_verdict": signal_verdict,
            # Updated by _match_idr() below every time an IDR is seen but
            # rejected for THIS entry (outside the accepted -max_early_s /
            # +match_window_s window) -- the closest such candidate's
            # signed offset in ms (positive = late, negative = early), so
            # an eventual MISSED verdict can say WHY: a genuine "nothing
            # nearby at all" vs. "something WAS nearby, just outside the
            # accepted window". None until/unless such a candidate is seen.
            "closest_rejected_diff_ms": None,
        }
        self.pending.append(entry)

        # -- Reference-frame snapshots (see the __init__ comment for the
        # exact semantics of each mode) --------------------------------
        if self.snapshot_dir and self.time_to_event_snapshot == "cue_arrival_frame" \
                and self.last_video_pts_ticks is not None:
            # "The frame on air when the cue arrived" -- the most recently
            # observed video AU's PTS at this exact instant.
            self._enqueue_arbitrary_frame_snapshot(
                self.last_video_pts_ticks, seq, "time_to_event_cue_arrival")
        if self.time_to_event_snapshot == "target_pts_frame":
            # "The frame at the literal target PTS" -- not known yet (the
            # target is, by definition, in the future relative to the cue),
            # so just flag this entry; _process_video_pes() captures it the
            # moment a video AU at/after target_ticks is actually observed.
            entry["target_pts_frame_pending"] = True
        if self.snapshot_dir and self.preroll_snapshot == "realtime_deadline_frame" \
                and time_to_event_ms is not None and time_to_event_ms > 0:
            # Schedule a wall-clock capture for the moment this cue's own
            # DECLARED lead time elapses -- i.e. the real-time deadline a
            # downstream splicer would have to act by if it trusted
            # time_to_event_ms. If time_to_event_ms is unknown or already
            # non-positive (cue arrived with the target already at/behind
            # the live position), there's no meaningful future deadline to
            # schedule against, so this mode is skipped for this cue.
            timer = threading.Timer(
                time_to_event_ms / 1000.0, self._fire_preroll_deadline_frame, args=(seq,))
            timer.daemon = True
            self._reference_snapshot_timers.append(timer)
            timer.start()
        # Full detail goes to the dedicated SCTE-35 channel; the main log
        # stream stays focused on lifecycle (PAT/PMT/join) and the eventual
        # match/miss verdict in _emit(), so it doesn't get drowned out on a
        # busy SCTE-35 PID.
        self.scte35_logger.info(
            "SCTE-35 #%d queued for matching: %s event_id=%s target_pts=%.6fs "
            "(raw pts_time=%.6f, pts_adjustment_ticks=%s) time_to_event=%s "
            "signal_verdict=%s descriptors=[%s]",
            seq, command_type, event_id, target_ticks / PTS_HZ, pts_time,
            pts_adjustment_ticks,
            "n/a" if time_to_event_ms is None else f"{time_to_event_ms:.1f}ms",
            signal_verdict,
            "; ".join(segmentation_summary))
        if signal_verdict == "SIGNAL_LATE":
            # Worth surfacing immediately, not just once the eventual
            # match/miss resolves (which may be seconds away, or never, if
            # the splice is later missed) -- an operator watching the main
            # log should see this as soon as it's known.
            self.scte35_logger.warning(
                "SCTE-35 #%d event_id=%s: first-signaled time_to_event=%.1fms "
                "is BELOW the %.0fms SCTE-35 minimum advance-notice "
                "requirement (see README) -- signal_verdict=SIGNAL_LATE",
                seq, event_id, time_to_event_ms, self.min_time_to_event_ms)

    # -- Video / IDR handling --------------------------------------------

    def handle_video(self, payload, pusi):
        result = self.pes_video.push(payload, pusi)
        if result:
            self._process_video_pes(result)

    def _process_video_pes(self, parsed):
        pts, dts, es_payload = parsed
        if self.video_codec not in ("h264", "hevc"):
            return
        if pts is not None:
            # Track the live video position from EVERY access unit (not
            # just IDRs) -- this is the reference point _register_cue()
            # uses to compute each cue's declared time-to-event.
            self.last_video_pts_ticks = pts
        nal_types = list(find_nal_types(es_payload, self.video_codec))
        if self.snapshot_dir:
            # Keep the most recently seen parameter sets warm from EVERY
            # access unit (not just IDRs) so a lone IDR can still be
            # decoded standalone even on a repeatHeaders=0 stream that only
            # sends SPS/PPS once, at the very start.
            params = extract_param_set_nals(es_payload, self.video_codec)
            if params:
                self.last_param_sets = params
        kind = classify_idr(nal_types, self.video_codec, self.include_cra)
        # Buffer EVERY access unit (in transport/decode order) when
        # --pre-frames is enabled, BEFORE the "not an IDR -> return" early
        # exit below, since the whole point is to also have the non-IDR
        # frames around. pts is normally present on every AU; if it isn't
        # (shouldn't happen for a compliant stream) we still buffer with
        # pts=None so the deque stays contiguous, it just can't become an
        # IDR target itself.
        if self.au_buffer is not None:
            self.au_buffer.append({
                "pts": pts,
                "data": es_payload,
                "kind": kind,
                "has_params": bool(has_param_sets(es_payload, self.video_codec)),
            })
        # target_pts_frame reference-snapshot mode: checked against EVERY
        # access unit (not just IDRs), since the target PTS a cue points to
        # is essentially never itself an IDR. Captures the first AU whose
        # PTS lands at or after the cue's target_ticks -- "the frame this
        # cue's target PTS actually points to" -- once, per pending entry.
        if (self.snapshot_dir and self.time_to_event_snapshot == "target_pts_frame"
                and pts is not None):
            for entry in self.pending:
                if not entry.get("target_pts_frame_pending") or entry.get("target_pts_frame_done"):
                    continue
                if pts_diff_seconds(pts, entry["target_ticks"]) >= 0:
                    entry["target_pts_frame_done"] = True
                    self._enqueue_arbitrary_frame_snapshot(
                        pts, entry["cue_seq"], "time_to_event_target_pts")
        if kind is None:
            return
        if pts is None:
            log.warning("IDR/CRA access unit found with no PTS in its PES "
                        "header -- cannot match against SCTE-35 (kind=%s)", kind)
            return
        # Snapshot BEFORE matching, using the current (pre-match) pending
        # state: any IDR that is about to match a pending splice point
        # necessarily has self.pending non-empty right now, so this always
        # captures splice-relevant IDRs even without --snapshot-all-idr.
        if self.snapshot_dir and self.snapshot_matched_idr_enabled and (self.snapshot_all_idr or self.pending):
            if self.pre_frames > 0:
                self._enqueue_snapshot_sequence(pts, es_payload)
            else:
                self._enqueue_snapshot(pts, es_payload)
        self._match_idr(pts, kind)

    def _idr_snapshot_path(self, idr_ticks):
        return os.path.join(self.snapshot_dir,
                             f"idr_pts_{idr_ticks / PTS_HZ:.6f}_{self.video_codec}.jpg")

    def _pre_frame_snapshot_path(self, idr_ticks, offset, frame_pts):
        pts_label = f"{frame_pts / PTS_HZ:.6f}" if frame_pts is not None else "unknown"
        return os.path.join(
            self.snapshot_dir,
            f"idr_pts_{idr_ticks / PTS_HZ:.6f}_{self.video_codec}_pre{offset}_pts_{pts_label}.jpg")

    def _reference_snapshot_path(self, cue_seq, tag, frame_pts):
        pts_label = f"{frame_pts / PTS_HZ:.6f}" if frame_pts is not None else "unknown"
        return os.path.join(
            self.snapshot_dir,
            f"cue_{cue_seq}_{tag}_pts_{pts_label}_{self.video_codec}.jpg")

    def _enqueue_arbitrary_frame_snapshot(self, anchor_pts_ticks, cue_seq, tag):
        """Decode and save a JPEG of one specific, already-buffered access
        unit -- unlike _enqueue_snapshot_sequence() (always anchored on the
        just-arrived IDR that triggered it), the target frame here can be
        ANY access unit already sitting in self.au_buffer, IDR or not, found
        by exact PTS match. Used by the time_to_event/pre-roll reference-
        snapshot modes below. Silently gives up (with a log line) if the
        buffer doesn't contain that PTS -- e.g. au_buffer_size too small, or
        (for target_pts_frame) the target PTS never actually showed up in
        the video."""
        if not self.snapshot_dir or self.au_buffer is None:
            return
        buf = list(self.au_buffer)
        anchor_idx = None
        for i in range(len(buf) - 1, -1, -1):
            if buf[i]["pts"] == anchor_pts_ticks:
                anchor_idx = i
                break
        if anchor_idx is None:
            log.debug("Reference snapshot (%s, cue_seq=%s): pts=%.6fs not found "
                      "in the access-unit buffer -- skipping.",
                      tag, cue_seq, anchor_pts_ticks / PTS_HZ if anchor_pts_ticks is not None else -1)
            return
        chunk_start = 0
        for i in range(anchor_idx - 1, -1, -1):
            if buf[i]["kind"] in ("idr", "cra"):
                chunk_start = i
                break
        chunk = buf[chunk_start:anchor_idx + 1]
        first = chunk[0]
        prefix = b"" if first["has_params"] else self.last_param_sets
        decode_bytes = prefix + b"".join(au["data"] for au in chunk)
        ranked = sorted(
            (au for au in chunk if au["pts"] is not None),
            key=lambda au: au["pts"])
        positions = [i for i, au in enumerate(ranked) if au["pts"] == anchor_pts_ticks]
        if not positions:
            log.warning("Reference snapshot (%s, cue_seq=%s): anchor frame missing "
                        "from its own decode chunk after sorting -- skipping.", tag, cue_seq)
            return
        path = self._reference_snapshot_path(cue_seq, tag, anchor_pts_ticks)
        self.snapshot_queue.put({
            "kind": "reference",
            "decode_bytes": decode_bytes,
            "codec": self.video_codec,
            "total_frames": len(ranked),
            "rank_pos": positions[-1],
            "path": path,
            "cue_seq": cue_seq,
            "tag": tag,
            "frame_pts": anchor_pts_ticks,
        })

    def _fire_preroll_deadline_frame(self, cue_seq):
        """threading.Timer callback, scheduled from _register_cue() to fire
        at the real wall-clock moment the cue's own declared time_to_event_ms
        elapses -- captures whatever frame is on air (self.last_video_pts_
        ticks) at that instant. Runs on the Timer's own thread; reads
        self.last_video_pts_ticks and self.au_buffer, both otherwise only
        ever written/appended from the ingest thread. CPython's GIL makes
        the individual read/append operations involved atomic, so this is
        safe in the same best-effort sense the rest of this diagnostic
        tool's threading already relies on (see the snapshot worker thread)
        -- worst case this fires a frame or two off from the true deadline
        under heavy contention, never a crash."""
        if not self.snapshot_dir or self.au_buffer is None:
            return
        anchor = self.last_video_pts_ticks
        if anchor is None:
            return
        self._enqueue_arbitrary_frame_snapshot(anchor, cue_seq, "preroll_realtime_deadline")

    def _enqueue_snapshot(self, idr_ticks, es_payload):
        path = self._idr_snapshot_path(idr_ticks)
        if has_param_sets(es_payload, self.video_codec):
            data = es_payload  # already self-contained (e.g. repeatHeaders=1)
        elif self.last_param_sets:
            data = self.last_param_sets + es_payload
        else:
            log.debug("No cached SPS/PPS yet for snapshot %s -- ffmpeg may "
                      "fail to decode this one standalone.", path)
            data = es_payload
        self.snapshot_queue.put({"kind": "single", "path": path, "data": data,
                                  "codec": self.video_codec})

    def _enqueue_snapshot_sequence(self, idr_ticks, es_payload):
        """Enqueue a task that decodes the matched IDR PLUS up to
        self.pre_frames preceding frames (in display/PTS order) as separate
        JPEGs. P/B frames are not independently decodable, so we hand
        ffmpeg a complete, valid chunk: everything in self.au_buffer from
        the PREVIOUS IDR/CRA (a guaranteed GOP boundary) up to and
        including this IDR, decoded once, then we keep only the frames
        actually wanted."""
        buf = list(self.au_buffer) if self.au_buffer is not None else []
        # Find this IDR's own slot -- search from the end since it was just
        # appended; match on PTS (identity would also work but PTS is what
        # every caller already has).
        idr_idx = None
        for i in range(len(buf) - 1, -1, -1):
            if buf[i]["pts"] == idr_ticks and buf[i]["kind"] in ("idr", "cra"):
                idr_idx = i
                break
        if idr_idx is None:
            log.warning("Could not locate matched IDR (pts=%.6fs) in the "
                        "access-unit buffer -- falling back to a single-"
                        "frame snapshot.", idr_ticks / PTS_HZ)
            self._enqueue_snapshot(idr_ticks, es_payload)
            return
        # Walk backward from just before the IDR to find the previous
        # IDR/CRA to use as the decodable chunk's starting point. If none
        # is found (e.g. very start of the buffer/stream), fall back to
        # starting the chunk at the buffer's own beginning -- best effort;
        # ffmpeg will simply produce fewer usable leading frames if some of
        # those AUs turn out to reference frames older than the chunk.
        chunk_start = 0
        for i in range(idr_idx - 1, -1, -1):
            if buf[i]["kind"] in ("idr", "cra"):
                chunk_start = i
                break
        chunk = buf[chunk_start:idr_idx + 1]
        if len(chunk) <= 1:
            # Nothing usable before the IDR itself (e.g. stream start) --
            # just do the plain single-frame snapshot.
            self._enqueue_snapshot(idr_ticks, es_payload)
            return
        first = chunk[0]
        prefix = b"" if first["has_params"] else self.last_param_sets
        decode_bytes = prefix + b"".join(au["data"] for au in chunk)
        # ffmpeg's decoder emits frames in DISPLAY (PTS) order, so sort the
        # chunk the same way to know which decoded-frame index corresponds
        # to which access unit.
        ranked = sorted(
            (au for au in chunk if au["pts"] is not None),
            key=lambda au: au["pts"])
        idr_positions = [i for i, au in enumerate(ranked) if au["pts"] == idr_ticks]
        if not idr_positions:
            log.warning("Matched IDR (pts=%.6fs) missing from its own decode "
                        "chunk after sorting -- falling back to a single-"
                        "frame snapshot.", idr_ticks / PTS_HZ)
            self._enqueue_snapshot(idr_ticks, es_payload)
            return
        idr_pos = idr_positions[-1]
        lo = max(0, idr_pos - self.pre_frames)
        targets = []
        for rank_pos in range(lo, idr_pos + 1):
            offset = idr_pos - rank_pos  # 0 = the IDR itself, 1 = immediately before, ...
            frame_pts = ranked[rank_pos]["pts"]
            path = (self._idr_snapshot_path(idr_ticks) if offset == 0
                    else self._pre_frame_snapshot_path(idr_ticks, offset, frame_pts))
            targets.append({"rank_pos": rank_pos, "path": path})
        if lo > 0:
            log.debug("Only %d frame(s) available before IDR pts=%.6fs in the "
                      "buffer (requested %d) -- capturing what's available.",
                      idr_pos - lo, idr_ticks / PTS_HZ, self.pre_frames)
        # targets is ordered oldest -> newest, ending with the IDR itself
        # (offset 0); record everything but that last one as the "pre
        # frame" paths for _emit() to surface once matching completes.
        self._pending_pre_frame_paths[idr_ticks] = [t["path"] for t in targets[:-1]]
        while len(self._pending_pre_frame_paths) > 2000:
            self._pending_pre_frame_paths.popitem(last=False)
        self.snapshot_queue.put({
            "kind": "sequence",
            "decode_bytes": decode_bytes,
            "codec": self.video_codec,
            "total_frames": len(ranked),
            "targets": targets,
            "idr_ticks": idr_ticks,
        })

    def _snapshot_worker(self):
        """Runs in a background thread so decoding a frame to JPEG (an
        ffmpeg subprocess call, tens of milliseconds) never blocks the
        socket-receive loop -- a stall there risks dropping multicast
        packets, which matters far more than a snapshot landing a moment
        late."""
        input_format = {"h264": "h264", "hevc": "hevc"}
        while True:
            item = self.snapshot_queue.get()
            if item is None:
                self.snapshot_queue.task_done()
                break
            try:
                if item.get("kind") == "sequence":
                    self._run_sequence_snapshot(item, input_format)
                elif item.get("kind") == "reference":
                    self._run_reference_snapshot(item, input_format)
                else:
                    self._run_single_snapshot(item, input_format)
            except Exception as exc:  # noqa: BLE001 -- keep the worker alive
                log.warning("Snapshot failed: %s", exc)
            finally:
                self.snapshot_queue.task_done()

    def _run_single_snapshot(self, item, input_format):
        path, data, codec = item["path"], item["data"], item["codec"]
        fmt = input_format.get(codec)
        if fmt is None:
            log.warning("Snapshot failed for %s: unsupported codec %s", path, codec)
            return
        proc = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", fmt, "-i", "pipe:0",
             "-frames:v", "1", "-q:v", "2", path],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10,
        )
        if proc.returncode != 0 or not os.path.exists(path):
            log.warning("Snapshot failed for %s (ffmpeg exit %s): %s",
                        path, proc.returncode,
                        proc.stderr.decode(errors="replace").strip()[-300:])
        else:
            log.debug("Saved IDR snapshot: %s", path)
            self._emit_event("snapshot_saved", {"path": path, "kind": "single"})

    def _run_sequence_snapshot(self, item, input_format):
        codec = item["codec"]
        targets = item["targets"]
        fmt = input_format.get(codec)
        if fmt is None:
            log.warning("Sequence snapshot failed: unsupported codec %s", codec)
            return
        with tempfile.TemporaryDirectory(prefix="scte35_idr_seq_") as tmpdir:
            pattern = os.path.join(tmpdir, "f_%06d.jpg")
            proc = subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-f", fmt, "-i", "pipe:0",
                 "-vsync", "0", "-q:v", "2", pattern],
                input=item["decode_bytes"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=20,
            )
            if proc.returncode != 0:
                log.warning("Sequence snapshot decode failed (ffmpeg exit %s): %s",
                            proc.returncode,
                            proc.stderr.decode(errors="replace").strip()[-300:])
                return
            for target in targets:
                # ffmpeg numbers its output frames 1-based, in the same
                # (display/PTS) order we sorted `ranked` into, so
                # rank_pos (0-based) -> ffmpeg frame number rank_pos + 1.
                src = os.path.join(tmpdir, f"f_{target['rank_pos'] + 1:06d}.jpg")
                dest = target["path"]
                if not os.path.exists(src):
                    log.warning("Sequence snapshot missing expected frame %s "
                                "(decoded %s frame(s), wanted rank %d) for %s",
                                src, item.get("total_frames"), target["rank_pos"], dest)
                    continue
                shutil.copyfile(src, dest)
                log.debug("Saved snapshot: %s", dest)
                self._emit_event("snapshot_saved", {
                    "path": dest, "kind": "sequence",
                    "idr_ticks": item.get("idr_ticks"),
                    "offset": target["rank_pos"],
                })

    def _run_reference_snapshot(self, item, input_format):
        """Counterpart to _run_sequence_snapshot for a single arbitrary
        reference frame (see _enqueue_arbitrary_frame_snapshot): decodes the
        whole GOP-chunk once (ffmpeg emits frames in display/PTS order) and
        keeps only the one frame at item['rank_pos']."""
        codec = item["codec"]
        fmt = input_format.get(codec)
        if fmt is None:
            log.warning("Reference snapshot failed: unsupported codec %s", codec)
            return
        with tempfile.TemporaryDirectory(prefix="scte35_ref_") as tmpdir:
            pattern = os.path.join(tmpdir, "f_%06d.jpg")
            proc = subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-f", fmt, "-i", "pipe:0",
                 "-vsync", "0", "-q:v", "2", pattern],
                input=item["decode_bytes"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=20,
            )
            if proc.returncode != 0:
                log.warning("Reference snapshot decode failed (ffmpeg exit %s): %s",
                            proc.returncode,
                            proc.stderr.decode(errors="replace").strip()[-300:])
                return
            src = os.path.join(tmpdir, f"f_{item['rank_pos'] + 1:06d}.jpg")
            dest = item["path"]
            if not os.path.exists(src):
                log.warning("Reference snapshot missing expected frame %s "
                            "(decoded %s frame(s), wanted rank %d) for %s",
                            src, item.get("total_frames"), item["rank_pos"], dest)
                return
            shutil.copyfile(src, dest)
            log.debug("Saved reference snapshot (%s): %s", item.get("tag"), dest)
            self._emit_event("reference_snapshot_saved", {
                "path": dest, "cue_seq": item.get("cue_seq"), "tag": item.get("tag"),
                "frame_pts": item.get("frame_pts"),
            })

    def _match_idr(self, idr_ticks, kind):
        now = time.monotonic()
        # drop timed-out pending entries and report them as missed
        still_pending = []
        for entry in self.pending:
            if now > entry["deadline"]:
                self._emit(entry, idr_ticks=None, kind=None, missed=True, miss_reason="timeout")
            else:
                still_pending.append(entry)
        self.pending = still_pending

        best = None
        best_diff = None
        for entry in self.pending:
            diff_s = pts_diff_seconds(idr_ticks, entry["target_ticks"])
            # Only accept an IDR at/after (target - max_early_s), never
            # arbitrarily far before it. A forced/reactive IDR cannot be
            # caused by a cue it hasn't been told about yet -- an IDR that
            # lands, say, 2.6s *before* the target is essentially always an
            # unrelated periodic IDR (from a non-zero intraPeriod, or a
            # gopPresetIdx GOP boundary) that happened to fall inside a
            # generous symmetric window, not a reaction to this cue. Without
            # this floor, the old symmetric "abs(diff) <= window" check would
            # greedily grab the first nearby IDR in processing order even
            # when it obviously precedes the cue by seconds, producing a
            # nonsensical large negative delta instead of waiting for the
            # real (later) candidate or timing out.
            if -self.max_early_s <= diff_s <= self.match_window_s:
                if best is None or abs(diff_s) < abs(best_diff):
                    best, best_diff = entry, diff_s
            else:
                # Outside the acceptance window -- not a match candidate, but
                # track the closest such IDR seen while this cue was still
                # pending purely as a MISSED-verdict diagnostic (see _emit):
                # lets us tell "an IDR WAS nearby, just too early/late to
                # match" apart from "nothing at all was anywhere close",
                # which is exactly the ambiguity that makes a MISSED event
                # hard to triage by eye when comparing an incoming feed
                # against its packaged/outgoing counterpart.
                diff_ms = diff_s * 1000.0
                prev = entry.get("closest_rejected_diff_ms")
                if prev is None or abs(diff_ms) < abs(prev):
                    entry["closest_rejected_diff_ms"] = diff_ms
        if best is not None:
            self.pending.remove(best)
            self._emit(best, idr_ticks=idr_ticks, kind=kind, missed=False)

    def flush_pending_as_missed(self, reason="eof"):
        """Called once at shutdown -- Ctrl-C on a live capture, or reaching
        the end of --input-file -- for any splice point still sitting in
        self.pending at that point: it never got a matching IDR before the
        run ended, so it's reported as MISSED here instead of just quietly
        vanishing from the CSV/JSON/console output with no record at all.
        This does NOT wait for --timeout-s to actually elapse (there may be
        nothing left to wait on, e.g. --input-file has no more data coming)
        -- reaching the end of input while a cue is still open is itself
        conclusive: no matching IDR arrived. `reason` is passed through to
        _emit() to produce an accurate verdict string (a genuine mid-stream
        timeout reads differently than "we ran out of input")."""
        still_pending = self.pending
        self.pending = []
        for entry in still_pending:
            self._emit(entry, idr_ticks=None, kind=None, missed=True, miss_reason=reason)

    def _emit(self, entry, idr_ticks, kind, missed, miss_reason="timeout"):
        wallclock = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        if missed:
            delta_ms = None
            base_reason = ("input ended before a matching IDR was found"
                            if miss_reason == "eof" else
                            "no IDR near target PTS within timeout")
            # If _match_idr() ever saw an IDR for this cue that fell outside
            # the accepted window, say so and how far off it was -- turns a
            # bare "MISSED" into an actionable diagnostic: a candidate that
            # was e.g. 340ms too late to match (encoder/GOP jitter pushing
            # it past --tolerance-ms) reads very differently from no nearby
            # IDR existing at all (an encoder that dropped/ignored the cue
            # entirely). See closest_rejected_diff_ms in _register_cue/
            # _match_idr. Appended rather than replacing the base text so
            # existing exact-string assertions on the no-candidate case
            # (closest_rejected_diff_ms stays None) are unaffected.
            closest_rejected_diff_ms = entry.get("closest_rejected_diff_ms")
            if closest_rejected_diff_ms is not None:
                direction = "late" if closest_rejected_diff_ms > 0 else "early"
                base_reason += (
                    f" -- nearest IDR seen was {abs(closest_rejected_diff_ms):.0f}ms too "
                    f"{direction} to match, outside the accepted window"
                )
            verdict = f"MISSED ({base_reason})"
            idr_pts_s = None
        else:
            delta_ms = pts_diff_seconds(idr_ticks, entry["target_ticks"]) * 1000.0
            idr_pts_s = idr_ticks / PTS_HZ
            if kind == "cra":
                verdict = "REVIEW (splice point lands on CRA, not IDR)"
            elif abs(delta_ms) <= self.ok_threshold_ms:
                verdict = "OK"
            else:
                verdict = "OUT_OF_SPEC"
        target_pts_s = entry["target_ticks"] / PTS_HZ
        snapshot_path = (self._idr_snapshot_path(idr_ticks)
                         if (self.snapshot_dir and self.snapshot_matched_idr_enabled and not missed) else None)
        pre_frame_paths = (self._pending_pre_frame_paths.pop(idr_ticks, [])
                           if (self.snapshot_dir and self.snapshot_matched_idr_enabled and not missed) else [])
        if snapshot_path and self.preroll_snapshot == "same_as_matched_idr":
            # "Same frame as the matched IDR snapshot" -- no extra decode
            # work, just relabel the snapshot _enqueue_snapshot()/
            # _enqueue_snapshot_sequence() already queued for this IDR as
            # this cue's pre-roll reference too. Emitted here (synchronously,
            # possibly before that JPEG has actually finished writing on the
            # snapshot worker thread) for the same reason match_result
            # already carries snapshot_path before the file is guaranteed to
            # exist -- consistent with the rest of this event stream.
            self._emit_event("reference_snapshot_saved", {
                "path": snapshot_path, "cue_seq": entry.get("cue_seq"),
                "tag": "preroll_same_as_matched_idr", "frame_pts": idr_ticks,
            })

        # -- Declared vs. actual pre-roll --------------------------------
        # time_to_event_ms: the DECLARED lead time, computed once at cue
        # registration (see _register_cue) from target_ticks minus the
        # video PTS observed at that moment -- a PTS-domain, "what the
        # cue itself claims" figure.
        # actual_preroll_ms: the REAL, wall-clock elapsed time between
        # registering the cue and actually observing/matching the IDR --
        # this is what a downstream ad-decisioning system genuinely got
        # to react in, regardless of what the PTS math promised. Only
        # meaningful for an actual match (a MISSED cue never got an IDR to
        # measure against); it also assumes the feed is arriving live/in
        # real time -- see the "Time to event vs. actual pre-roll" README
        # section for why a non-real-time replay invalidates this figure.
        time_to_event_ms = entry.get("time_to_event_ms")
        actual_preroll_ms = ((time.monotonic() - entry["register_monotonic"]) * 1000.0
                             if not missed else None)
        preroll_delta_ms = (None if (time_to_event_ms is None or actual_preroll_ms is None)
                            else actual_preroll_ms - time_to_event_ms)
        if preroll_delta_ms is None:
            preroll_verdict = "N/A"
        elif preroll_delta_ms < -self.preroll_tolerance_ms:
            # Actual delivered lead time fell short of what the cue's own
            # PTS declared -- the operationally important case: downstream
            # ad-decisioning/splicing may have gotten less warning than it
            # was promised.
            preroll_verdict = "PREROLL_SHORT"
        else:
            preroll_verdict = "OK"

        # signal_verdict was already decided at registration time (see
        # _register_cue) -- whether THIS event's first transmission met
        # SCTE-35's own minimum advance-notice requirement. Carried through
        # here unchanged so it rides along with the rest of the match/miss
        # outcome in every output channel.
        signal_verdict = entry.get("signal_verdict", "N/A")

        # -- gop_verdict: does this delta look like a genuinely forced
        # keyframe at the splice point, or like the encoder just fell
        # through to its next naturally-scheduled GOP boundary instead? A
        # downstream splicer/packager can only cut on an IDR, so if the
        # encoder isn't forcing one exactly at target_pts, delta_ms will
        # cluster around whole multiples of the stream's own GOP duration
        # (the periodically ffprobe-sampled avg_gop_length_frames / fps --
        # see _sample_video_info) rather than around zero. This is the
        # diagnostic for "ads start later than they should": a small
        # delta_ms (already verdict=OK) means the splice point itself was
        # honored, so any remaining lateness is downstream of this probe
        # (packager/ad-decisioning); a delta_ms landing on ~1x/2x/3x the
        # GOP duration means the encoder itself never gave the packager a
        # clean cut point to work with, and that's the root cause to chase.
        # Only evaluated for an actual match with delta_ms available --
        # never meant to override `verdict` above, just to explain it.
        if missed or delta_ms is None or not self._last_gop_duration_ms:
            gop_verdict = "N/A"
        else:
            gop_ms = self._last_gop_duration_ms
            n = round(delta_ms / gop_ms)
            tolerance_ms = min(0.2 * gop_ms, 200.0)
            if n >= 1 and abs(delta_ms - n * gop_ms) <= tolerance_ms:
                gop_verdict = "GOP_WAIT"
            elif abs(delta_ms) <= self.ok_threshold_ms:
                gop_verdict = "FORCED"
            else:
                gop_verdict = "UNCLEAR"

        # near_miss_ms: surfaces the same closest_rejected_diff_ms diagnostic
        # used to build the MISSED verdict text above as its own machine-
        # readable field (CSV/JSON/DB/GUI), rather than forcing every
        # consumer to parse it back out of the verdict string. Only ever
        # set for a MISSED entry that had a rejected near-candidate; None
        # otherwise (including for a genuine match, where it's moot).
        near_miss_ms = entry.get("closest_rejected_diff_ms") if missed else None

        line = (f"[{wallclock}] event_id={entry['event_id']} "
                f"type={entry['command_type']} oon={entry['out_of_network']} "
                f"target_pts={target_pts_s:.6f}s "
                f"idr_pts={'n/a' if idr_pts_s is None else f'{idr_pts_s:.6f}s'} "
                f"delta={'n/a' if delta_ms is None else f'{delta_ms:+.1f}ms'} "
                f"verdict={verdict}"
                + (f" snapshot={snapshot_path}" if snapshot_path else "")
                + (f" pre_frames={len(pre_frame_paths)}" if pre_frame_paths else "")
                + (f" time_to_event={'n/a' if time_to_event_ms is None else f'{time_to_event_ms:.1f}ms'}"
                   f" actual_preroll={'n/a' if actual_preroll_ms is None else f'{actual_preroll_ms:.1f}ms'}"
                   f" preroll_verdict={preroll_verdict} signal_verdict={signal_verdict}"
                   f" gop_verdict={gop_verdict}"))
        (log.warning if missed or (delta_ms is not None and abs(delta_ms) > self.ok_threshold_ms)
         or preroll_verdict == "PREROLL_SHORT" or signal_verdict == "SIGNAL_LATE"
         or gop_verdict == "GOP_WAIT" else log.info)(line)

        if self.csv_writer:
            self.csv_writer.writerow([
                wallclock, entry.get("cue_seq"), entry["event_id"], entry["command_type"],
                entry["out_of_network"], f"{target_pts_s:.6f}",
                entry.get("raw_pts_time"), entry.get("pts_adjustment"),
                "" if idr_pts_s is None else f"{idr_pts_s:.6f}",
                "" if delta_ms is None else f"{delta_ms:.1f}",
                verdict, self.video_codec, kind,
                "; ".join(entry.get("segmentation_summary") or []),
                snapshot_path or "",
                "; ".join(pre_frame_paths),
                "" if time_to_event_ms is None else f"{time_to_event_ms:.1f}",
                "" if actual_preroll_ms is None else f"{actual_preroll_ms:.1f}",
                "" if preroll_delta_ms is None else f"{preroll_delta_ms:.1f}",
                preroll_verdict,
                signal_verdict,
                gop_verdict,
                "" if near_miss_ms is None else f"{near_miss_ms:.1f}",
            ])
            self.csv_file.flush()

        # Built unconditionally (not just when --json-out is set) so the
        # scte35-analyzer live event hook always gets a full match/miss record --
        # this is the primary "marker" event the GUI's live table/timeline
        # is driven from.
        record = {
            "tool_version": __version__,
            "wallclock": wallclock, "cue_seq": entry.get("cue_seq"),
            "event_id": entry["event_id"],
            "command_type": entry["command_type"],
            "out_of_network": entry["out_of_network"],
            "target_pts_s": target_pts_s,
            "raw_pts_time_s": entry.get("raw_pts_time"),
            "pts_adjustment_ticks": entry.get("pts_adjustment"),
            "segmentation_summary": entry.get("segmentation_summary"),
            "idr_pts_s": idr_pts_s,
            "delta_ms": delta_ms, "verdict": verdict,
            "codec": self.video_codec, "au_kind": kind,
            "snapshot_path": snapshot_path,
            "pre_frame_snapshot_paths": pre_frame_paths,
            "time_to_event_ms": time_to_event_ms,
            "actual_preroll_ms": actual_preroll_ms,
            "preroll_delta_ms": preroll_delta_ms,
            "preroll_verdict": preroll_verdict,
            "signal_verdict": signal_verdict,
            "gop_verdict": gop_verdict,
            "near_miss_ms": near_miss_ms,
            # cross-reference with --scte35-out/the scte35_cue event using
            # cue_seq for the complete raw SCTE-35 structure (all
            # descriptor fields).
        }
        self._emit_event("match_result", record)
        if self.json_out:
            self.json_out.write(json.dumps(record, default=str) + "\n")
            self.json_out.flush()

    # -- Raw TS dump around SCTE-35 events --------------------------------

    def _ts_dump_note_event(self, seq):
        """Called for EVERY decoded SCTE-35 message (see handle_scte35),
        regardless of whether it carries a time-specified pts_time -- for
        the purpose of "was there SCTE-35 activity in this window at all",
        a splice_null or canceled event is still activity worth dumping
        raw TS around."""
        if not self.ts_dump_dir:
            return
        self._ts_dump_event_count += 1
        self._ts_dump_event_seqs.append(seq)

    def _ts_dump_maybe_rotate(self):
        now = time.monotonic()
        if now - self._ts_dump_window_start < self.ts_dump_window_s:
            return
        self._rotate_ts_dump_window(now)

    def _rotate_ts_dump_window(self, now):
        """End the current dump window: write it out (with the previous
        window prepended, unless --ts-dump-no-preroll) if it qualifies --
        --ts-dump-all, or at least one SCTE-35 message was decoded during
        it -- then start a fresh window. The just-ended window is always
        kept as `_ts_dump_prev` for one more rotation regardless of
        whether IT gets saved, so the next window (if it has an event)
        gets real pre-roll context even when the event landed right at the
        start of its own window."""
        had_event = self._ts_dump_event_count > 0
        if (self.ts_dump_all or had_event) and self._ts_dump_cur:
            payload = (bytes(self._ts_dump_prev) + bytes(self._ts_dump_cur)
                       if self.ts_dump_include_previous else bytes(self._ts_dump_cur))
            self._enqueue_ts_dump(payload, self._ts_dump_event_count,
                                   list(self._ts_dump_event_seqs),
                                   self._ts_dump_window_start_wall)
        self._ts_dump_prev = self._ts_dump_cur
        self._ts_dump_cur = bytearray()
        self._ts_dump_event_count = 0
        self._ts_dump_event_seqs = []
        self._ts_dump_window_start = now
        self._ts_dump_window_start_wall = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    def _enqueue_ts_dump(self, payload, event_count, event_seqs, window_start_wall):
        window_end_wall = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        self._ts_dump_seq += 1
        # Sequence number first, zero-padded, so filenames sort correctly
        # (and stay unique) even with a short --ts-dump-window that ends
        # more than one window inside the same wallclock second, or across
        # an NTP clock step.
        fname = (f"ts_dump_{self._ts_dump_seq:06d}_{window_end_wall.replace(':', '-')}"
                 f"_n{event_count}events.ts")
        path = os.path.join(self.ts_dump_dir, fname)
        meta = {
            "tool_version": __version__,
            "path": path,
            "window_start_wallclock": window_start_wall,
            "window_end_wallclock": window_end_wall,
            "window_s": self.ts_dump_window_s,
            "included_previous_window": self.ts_dump_include_previous,
            "event_count": event_count,
            "event_cue_seqs": event_seqs,
            "size_bytes": len(payload),
        }
        self.ts_dump_queue.put({"path": path, "data": bytes(payload), "meta": meta})

    def _ts_dump_worker(self):
        """Runs in a background thread so writing tens/hundreds of MB to
        disk never blocks the socket-receive loop."""
        while True:
            item = self.ts_dump_queue.get()
            if item is None:
                self.ts_dump_queue.task_done()
                break
            try:
                path, data, meta = item["path"], item["data"], item["meta"]
                with open(path, "wb") as f:
                    f.write(data)
                with open(path + ".json", "w") as f:
                    json.dump(meta, f, indent=2)
                log.info("Saved TS dump: %s (%d SCTE-35 event(s), %.1f MB)",
                          path, meta["event_count"], len(data) / 1e6)
                self._emit_event("segment_saved", meta)
                if self.ts_dump_max_files:
                    self._prune_ts_dumps()
            except Exception as exc:  # noqa: BLE001 -- keep the worker alive
                log.warning("Failed to write TS dump %s: %s", item.get("path"), exc)
            finally:
                self.ts_dump_queue.task_done()

    def _prune_ts_dumps(self):
        """Delete the oldest saved dumps (by filename, which sorts
        chronologically) once --ts-dump-max-files is exceeded, so a
        long-running probe on a busy SCTE-35 PID can't silently fill the
        disk."""
        try:
            files = sorted(f for f in os.listdir(self.ts_dump_dir)
                           if f.startswith("ts_dump_") and f.endswith(".ts"))
        except OSError:
            return
        excess = len(files) - self.ts_dump_max_files
        for fname in files[:max(0, excess)]:
            base = os.path.join(self.ts_dump_dir, fname)
            for p in (base, base + ".json"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            log.info("Pruned old TS dump (over --ts-dump-max-files=%d): %s",
                      self.ts_dump_max_files, base)

    # -- Detailed video stream info (scte35-analyzer addition) -----------

    def _video_info_worker(self):
        """Runs in a background thread: periodically samples the raw TS
        buffer built up in handle_ts_packet() and shells out to ffprobe on
        a short local temp file for stream-level detail (see the comment
        in __init__). Deliberately NOT triggered per-packet or per-event:
        ffprobe is far too slow to run that often, and a stream's technical
        parameters essentially never change mid-broadcast, so a slow
        periodic sample is all this needs -- a manual re-check is also
        available via the GUI's "Refresh video info" action (see
        JobManager.refresh_video_info), which just calls _sample_video_info
        directly on demand."""
        # Give the buffer a head start before the first attempt, so a job
        # that finishes almost immediately (e.g. a short uploaded file)
        # still gets one sample rather than none.
        if self._video_info_stop.wait(min(5.0, self.video_info_interval_s)):
            return
        while True:
            self._sample_video_info()
            if self._video_info_stop.wait(self.video_info_interval_s):
                return

    def _sample_video_info(self):
        """Best-effort, must never raise: called from the periodic worker
        thread above, and also directly (cross-process, via a command
        queue -- see jobs/manager.py) for an on-demand refresh."""
        if not self.video_info_enabled or self._video_info_buffer is None:
            return
        if len(self._video_info_buffer) < 500:
            return  # not enough buffered yet (~750 ms+ at typical bitrates) for a meaningful sample
        sample = b"".join(self._video_info_buffer)  # snapshot; buffer is only ever appended to elsewhere
        fd, tmp_path = tempfile.mkstemp(suffix=".ts", prefix="dai_video_info_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(sample)
            try:
                info = _ffprobe_video_info(tmp_path)
            except Exception:  # noqa: BLE001 -- keep the worker/caller alive regardless
                log.exception("video_info sampling failed")
                info = None
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        if info:
            fps = info.get("frame_rate_fps")
            avg_gop_frames = (info.get("gop") or {}).get("avg_gop_length_frames")
            if fps and avg_gop_frames:
                self._last_gop_duration_ms = (avg_gop_frames / fps) * 1000.0
            self._emit_event("video_info", info)

    # -- TS packet dispatch ------------------------------------------------

    def handle_ts_packet(self, pkt):
        if pkt[0] != SYNC_BYTE:
            return
        if self.ts_dump_dir:
            # Buffer the RAW packet -- every PID, before any filtering
            # below -- so a saved dump is a complete, standalone .ts file,
            # not just the PIDs this tool otherwise cares about.
            self._ts_dump_cur += pkt
            self._ts_dump_maybe_rotate()
        if self.video_info_enabled:
            # Same "every PID" reasoning as ts_dump above -- ffprobe needs
            # PAT/PMT alongside the video PID to identify the stream at
            # all. Cheap: the deque just holds a reference to this already-
            # allocated bytes object and evicts the oldest on overflow.
            self._video_info_buffer.append(pkt)
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        pusi = bool(pkt[1] & 0x40)
        adaptation_field_control = (pkt[3] >> 4) & 0x03
        pos = 4
        if adaptation_field_control in (0x02, 0x03):
            af_len = pkt[4]
            pos += 1 + af_len
        if adaptation_field_control in (0x01, 0x03) and pos < TS_PACKET_SIZE:
            payload = pkt[pos:]
        else:
            payload = b""
        if not payload:
            return

        if pid == 0x0000:
            self.handle_pat(payload, pusi)
        elif self.pmt_pid is not None and pid == self.pmt_pid:
            self.handle_pmt(payload, pusi)
        elif self.scte35_pid is not None and pid == self.scte35_pid:
            self.handle_scte35(payload, pusi)
        elif self.video_pid is not None and pid == self.video_pid:
            self.handle_video(payload, pusi)

    def close(self):
        for timer in self._reference_snapshot_timers:
            timer.cancel()
        if self.csv_file:
            self.csv_file.close()
        if self.json_out:
            self.json_out.close()
        if self.scte35_out:
            self.scte35_out.close()
        if self.snapshot_thread:
            self.snapshot_queue.put(None)
            self.snapshot_queue.join()
            self.snapshot_thread.join(timeout=15.0)
        if self.ts_dump_thread:
            # Flush whatever is left in the current (possibly partial)
            # window -- same qualification rule (event present, or
            # --ts-dump-all) as a normal rotation -- so Ctrl-C doesn't
            # silently drop an in-progress event window.
            self._rotate_ts_dump_window(time.monotonic())
            self.ts_dump_queue.put(None)
            self.ts_dump_queue.join()
            self.ts_dump_thread.join(timeout=60.0)
        if self.video_info_thread:
            self._video_info_stop.set()
            self.video_info_thread.join(timeout=15.0)


def extract_ts_packets(buf):
    """Split a byte buffer into full 188-byte TS packets, resyncing on 0x47
    if the buffer does not start aligned (e.g. after a dropped fragment or
    mid-stream join). Returns (list_of_packets, leftover_bytes)."""
    n = len(buf)
    i = 0
    packets = []
    while i + TS_PACKET_SIZE <= n:
        if buf[i] == SYNC_BYTE:
            packets.append(bytes(buf[i:i + TS_PACKET_SIZE]))
            i += TS_PACKET_SIZE
        else:
            i += 1
    return packets, buf[i:]


def process_ts_file(probe, path):
    """Read raw MPEG-TS packets from the local file at `path` and feed each
    one through probe.handle_ts_packet(), exactly like a live capture would
    -- this is the entirety of what --input-file does, factored out of
    main() so it's directly testable without a live socket, argparse, or
    threefive3 installed. No RTP stripping (a captured .ts file -- e.g.
    from --ts-dump-dir -- is raw TS by convention, unlike a live UDP feed
    which may be RTP-encapsulated) and no real-time pacing: the file is
    read as fast as disk I/O allows. See the --input-file help text and
    the "File input mode" README section for what that means for
    actual_preroll_ms/preroll_verdict/timeout-based MISSED verdicts.

    Returns the number of complete 188-byte TS packets processed. Raises
    OSError if the file can't be opened/read."""
    packets_seen = 0
    leftover = bytearray()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)  # 1 MiB at a time
            if not chunk:
                break
            leftover += chunk
            packets, leftover = extract_ts_packets(leftover)
            leftover = bytearray(leftover)
            for pkt in packets:
                packets_seen += 1
                probe.handle_ts_packet(pkt)
    if leftover:
        log.warning("%d trailing byte(s) at end of %s did not form a complete 188-byte TS "
                    "packet -- ignored (likely a truncated capture).", len(leftover), path)
    return packets_seen


def main():
    ap = argparse.ArgumentParser(
        description="Measure SCTE-35 splice-point PTS vs. video IDR PTS "
                    "delta live from a multicast MPEG-TS stream.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--addr", default=None,
                    help="Multicast group address, e.g. 239.1.1.1 (224.0.0.0-239.255.255.255 -- "
                         "an IP_ADD_MEMBERSHIP join is performed). Anything outside that range "
                         "(most usefully 127.0.0.1) is instead treated as a plain UNICAST address "
                         "for local testing: no group join is attempted, the tool just listens "
                         "for UDP sent directly to that address:port -- handy for pointing an "
                         "ffmpeg/tsp test stream at loopback without any real multicast network. "
                         "Required unless --input-file is given.")
    ap.add_argument("--port", default=None, type=int,
                    help="UDP port. Required unless --input-file is given.")
    ap.add_argument("--input-file", default=None,
                    help="Read raw MPEG-TS from this local file instead of a live multicast/UDP "
                         "source -- e.g. a capture produced by --ts-dump-dir, or any standard "
                         "188-byte-aligned .ts file (VLC/ffmpeg/TSDuck-compatible). Mutually "
                         "exclusive with --addr/--port; --iface/--transport/--duration are ignored "
                         "(with a warning) since there's no live socket to configure. The whole "
                         "file is read once, start to finish, as fast as disk I/O allows -- NOT "
                         "paced to its original real-time cadence -- then the tool exits on its "
                         "own (no Ctrl-C needed). Because of that, actual_preroll_ms/"
                         "preroll_verdict and the --timeout-s-based MISSED verdict do NOT reflect "
                         "genuine real-time delivery in this mode; any splice point still pending "
                         "when the file ends is reported as MISSED regardless of --timeout-s. See "
                         "'File input mode' in the README before relying on those specific fields "
                         "from a file-mode run.")
    ap.add_argument("--iface", default=None,
                    help="Local interface IP to join the group on. Only meaningful when --addr is "
                         "an actual multicast address -- ignored (with a warning) otherwise.")
    ap.add_argument("--transport", choices=["auto", "ts", "rtp"], default="auto",
                    help="Payload framing: raw MPEG-TS-over-UDP, RTP-encapsulated, or auto-detect")
    ap.add_argument("--program", type=int, default=None,
                    help="Program number to follow if the mux carries several (default: first in PAT)")
    ap.add_argument("--pid-video", type=lambda x: int(x, 0), default=None,
                    help="Override auto-detected video PID (e.g. 0x101)")
    ap.add_argument("--pid-scte35", type=lambda x: int(x, 0), default=None,
                    help="Override auto-detected SCTE-35 PID (e.g. 0x1F0)")
    ap.add_argument("--codec", choices=["h264", "hevc"], default=None,
                    help="Force video codec instead of relying on PMT stream_type")
    ap.add_argument("--tolerance-ms", type=float, default=6000.0,
                    help="How far AFTER the target PTS an IDR may land and still be accepted as "
                         "the candidate match (ms)")
    ap.add_argument("--max-early-ms", type=float, default=50.0,
                    help="How far BEFORE the target PTS an IDR may land and still be accepted "
                         "(ms). Keep this small: a forced/reactive IDR cannot be caused by a cue "
                         "it hasn't been told about yet, so an IDR seconds before the target is "
                         "essentially always an unrelated periodic IDR (from intraPeriod or a "
                         "GOP boundary), not a reaction to this cue -- raising this value re-opens "
                         "the tool to greedily matching those and reporting nonsensical large "
                         "negative deltas")
    ap.add_argument("--timeout-s", type=float, default=12.0,
                    help="How long to wait for a matching IDR before declaring a splice point missed")
    ap.add_argument("--ok-threshold-ms", type=float, default=41.0,
                    help="Delta below which alignment is reported OK (default ~1 frame at 24fps)")
    ap.add_argument("--preroll-tolerance-ms", type=float, default=500.0,
                    help="How far the ACTUAL (wall-clock measured) pre-roll may fall short of the "
                         "DECLARED time-to-event (computed from the cue's own PTS at registration) "
                         "before being flagged PREROLL_SHORT (default 500 ms). Only actual pre-roll "
                         "shorter than declared is flagged -- extra pre-roll is never a problem. See "
                         "'time_to_event_ms'/'actual_preroll_ms'/'preroll_delta_ms' in the "
                         "csv-out/json-out/console output and the README section on this measurement "
                         "for what it does and does not tell you.")
    ap.add_argument("--min-time-to-event-ms", type=float, default=4000.0,
                    help="Minimum DECLARED time-to-event (time_to_event_ms) a splice event's first "
                         "transmission must have to satisfy SCTE-35's own minimum advance-notice "
                         "requirement (per secondary sources citing ANSI/SCTE 35 (2019) 9.2/10.3.3: "
                         "'sent at least once a minimum of 4 seconds in advance of the desired splice "
                         "time' -- default 4000 ms; not independently verified against the primary "
                         "standard text, see the README). Below this, the FIRST occurrence of a given "
                         "splice_event_id is flagged signal_verdict=SIGNAL_LATE. Later retransmissions "
                         "of the same event_id are reported as RETRANSMISSION, not re-flagged, since "
                         "the requirement is about the initial signal, not every repeat as the splice "
                         "point approaches.")
    ap.add_argument("--include-cra", action="store_true",
                    help="Also match HEVC CRA pictures as candidate splice points (flagged for review)")
    ap.add_argument("--csv-out", default=None, help="Append match/miss outcomes to this CSV file")
    ap.add_argument("--json-out", default=None, help="Append match/miss outcomes as JSON-lines to this file")
    ap.add_argument("--scte35-out", default=None,
                    help="Append EVERY decoded SCTE-35 message (all fields, all descriptors, "
                         "including splice_null/canceled/immediate ones that never get matched "
                         "against an IDR) as JSON-lines to this file")
    ap.add_argument("--scte35-log-file", default=None,
                    help="Also write SCTE-35 log lines (registration + descriptor summaries) to "
                         "this separate human-readable text log file, in addition to the console "
                         "(when set, SCTE-35 lines stop duplicating onto the console -- everything "
                         "else, e.g. PAT/PMT/match/miss lines, still goes to the console as before)")
    ap.add_argument("--snapshot-dir", default=None,
                    help="Save a JPEG of the matched IDR access unit for every splice event to "
                         "this directory (requires ffmpeg on PATH; decodes just that one access "
                         "unit standalone, using cached SPS/PPS if the stream doesn't repeat them "
                         "before every IDR). By default only IDRs that occur while a SCTE-35 "
                         "splice point is pending get snapshotted, to keep volume proportional to "
                         "actual ad-break activity rather than the whole stream's GOP cadence -- "
                         "see --snapshot-all-idr to change that. Filename encodes the IDR's own "
                         "PTS; the match/miss CSV/JSON/console output references it as "
                         "'snapshot_path' so you can open the exact frame in question.")
    ap.add_argument("--snapshot-all-idr", action="store_true",
                    help="With --snapshot-dir, save EVERY IDR (and CRA, if --include-cra) in the "
                         "stream, not just ones near a pending splice point. High volume on a "
                         "normal GOP cadence -- one file roughly every few seconds, 24/7.")
    ap.add_argument("--pre-frames", type=int, default=0,
                    help="With --snapshot-dir, also save this many access units immediately "
                         "BEFORE the matched IDR, in display/PTS order, as additional JPEGs "
                         "(e.g. --pre-frames 3 saves 3 pre-splice frames plus the IDR itself, 4 "
                         "files total). Since P/B frames aren't independently decodable this "
                         "decodes the whole GOP chunk back to the previous IDR/CRA once per "
                         "snapshot -- fewer frames than requested are saved if the stream start "
                         "or buffer limit is reached first. Requires --snapshot-dir.")
    ap.add_argument("--au-buffer-size", type=int, default=None,
                    help="With --pre-frames, how many access units to keep buffered in transport "
                         "order (default: max(300, pre_frames * 15)). Raise this if your GOP size "
                         "is large enough that --pre-frames frames aren't reliably found within "
                         "the previous GOP boundary.")
    ap.add_argument("--ts-dump-dir", default=None,
                    help="Continuously chop the raw multicast feed (every PID) into fixed-length "
                         "windows and save a window's raw .ts bytes to this directory whenever at "
                         "least one SCTE-35 message was decoded during it (see --ts-dump-all to "
                         "save every window instead). Each saved dump gets a JSON sidecar with the "
                         "window's timing and the SCTE-35 cue_seq values seen -- joinable with "
                         "--scte35-out. No ffmpeg dependency -- this is a raw byte-exact copy, "
                         "playable directly in VLC/ffplay/TSDuck.")
    ap.add_argument("--ts-dump-window", type=parse_duration_seconds, default=60.0,
                    help="Length of each TS dump window, e.g. '30s', '90s', '2m', '1.5m' (bare "
                         "numbers are seconds). Default 60s. Mind the memory footprint: with "
                         "--ts-dump-no-preroll NOT set (the default), up to 2x this window's worth "
                         "of raw TS stays resident in RAM at all times (roughly "
                         "bitrate_bps * window_s * 2 / 8 bytes) -- e.g. a 20 Mbit/s feed with a "
                         "60s window keeps ~300 MB buffered continuously.")
    ap.add_argument("--ts-dump-all", action="store_true",
                    help="With --ts-dump-dir, save EVERY window, not just ones containing a "
                         "decoded SCTE-35 message. Turns this into a plain rolling raw-TS recorder "
                         "-- high disk usage, combine with --ts-dump-max-files.")
    ap.add_argument("--ts-dump-no-preroll", action="store_true",
                    help="With --ts-dump-dir, do NOT prepend the previous window to a saved dump. "
                         "Default is to prepend it, so an event near the start of a window still "
                         "has real context before it instead of an abrupt cut; disabling this "
                         "halves the resident memory footprint and the saved file size.")
    ap.add_argument("--ts-dump-max-files", type=int, default=None,
                    help="With --ts-dump-dir, delete the oldest saved dumps once this many exist, "
                         "so a busy SCTE-35 PID can't silently fill the disk. Default: unlimited -- "
                         "strongly recommended to set this for unattended/production runs.")
    ap.add_argument("--duration", type=float, default=None, help="Stop after N seconds (default: run forever)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    log.info("scte35_idr_diff.py version %s starting", __version__)

    if Cue is None:
        log.error("threefive3 not found. Install with: "
                  "pip install threefive3 --break-system-packages")
        sys.exit(1)

    if args.snapshot_dir and shutil.which("ffmpeg") is None:
        log.error("--snapshot-dir requires ffmpeg on PATH to decode IDR access units to JPEG. "
                  "Install with: sudo apt install ffmpeg")
        sys.exit(1)

    if args.pre_frames and not args.snapshot_dir:
        log.error("--pre-frames requires --snapshot-dir (it saves additional JPEGs alongside "
                  "the IDR snapshot).")
        sys.exit(1)

    if args.ts_dump_dir and args.ts_dump_window <= 0:
        log.error("--ts-dump-window must be > 0 (got %s)", args.ts_dump_window)
        sys.exit(1)

    # -- Input source: exactly one of --input-file, or --addr + --port. --
    if args.input_file:
        if args.addr or args.port:
            log.error("--input-file is mutually exclusive with --addr/--port -- pick one input "
                       "source (a live multicast/unicast feed, or a local MPEG-TS file).")
            sys.exit(1)
        if not os.path.isfile(args.input_file):
            log.error("--input-file %r not found (or not a regular file).", args.input_file)
            sys.exit(1)
        if args.iface:
            log.warning("--iface is ignored with --input-file (no network socket is opened).")
        if args.transport != "auto":
            log.warning("--transport is ignored with --input-file: file input is always read as "
                        "raw MPEG-TS (matching what --ts-dump-dir produces), never RTP-stripped.")
        if args.duration:
            log.warning("--duration is ignored with --input-file: the whole file is processed "
                        "once, start to finish, rather than for a fixed wall-clock time.")
    else:
        if not args.addr or not args.port:
            log.error("Either --input-file, or both --addr and --port, must be given.")
            sys.exit(1)

    probe = Probe(args)

    if args.input_file:
        # -- Batch mode: read a local MPEG-TS file through the exact same
        # Probe.handle_ts_packet() pipeline used for live capture (via
        # process_ts_file()), so CSV/JSON/console output is otherwise
        # identical -- just driven by disk reads instead of socket
        # recvfrom(). See process_ts_file()'s docstring and the
        # --input-file help text for what that means for
        # actual_preroll_ms/preroll_verdict/timeout-based MISSED verdicts.
        log.info("Reading MPEG-TS from file: %s", args.input_file)
        try:
            packets_seen = process_ts_file(probe, args.input_file)
        except OSError as exc:
            log.error("Failed to read --input-file %s: %s", args.input_file, exc)
            probe.flush_pending_as_missed(reason="eof")
            probe.close()
            sys.exit(1)
        probe.flush_pending_as_missed(reason="eof")
        probe.close()
        log.info("Finished reading file. %d TS packets processed.", packets_seen)
        return

    sock = open_multicast_socket(args.addr, args.port, args.iface)
    if is_multicast_ipv4(args.addr):
        log.info("Joined multicast %s:%d (iface=%s, transport=%s)",
                 args.addr, args.port, args.iface or "any", args.transport)
    else:
        log.info("Listening for unicast UDP on %s:%d (transport=%s)",
                 args.addr, args.port, args.transport)

    stop = {"flag": False}

    def _sigint(_sig, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sigint)

    start = time.monotonic()
    leftover = bytearray()
    transport_mode = args.transport
    packets_seen = 0

    try:
        while not stop["flag"]:
            if args.duration and (time.monotonic() - start) > args.duration:
                break
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue

            if transport_mode == "auto":
                transport_mode = "rtp" if looks_like_rtp(data) else "ts"
                log.info("Auto-detected transport: %s", transport_mode)
            if transport_mode == "rtp":
                data = strip_rtp(data)

            leftover += data
            packets, leftover = extract_ts_packets(leftover)
            leftover = bytearray(leftover)
            for pkt in packets:
                packets_seen += 1
                probe.handle_ts_packet(pkt)
    finally:
        # A splice point still pending here never got a matching IDR
        # before the run stopped (Ctrl-C) -- report it as MISSED instead
        # of silently dropping it from the output with no record at all.
        probe.flush_pending_as_missed(reason="timeout")
        probe.close()
        sock.close()
        log.info("Stopped. %d TS packets processed.", packets_seen)


if __name__ == "__main__":
    main()
