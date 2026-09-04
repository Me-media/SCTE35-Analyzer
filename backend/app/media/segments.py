"""On-demand remux of a saved raw-TS segment (a --ts-dump-dir-style capture,
byte-exact, every PID) into a fragmented MP4 that browsers can actually play
back in an HTML5 <video> tag.

Browsers have very inconsistent (mostly nonexistent) native support for
playing a raw .ts container directly via <video src="...">, even when the
elementary streams inside it (H.264/AAC) are otherwise completely standard.
Since a saved segment is being played back on demand, not streamed live, a
one-off `ffmpeg -c copy` remux (no re-encode, so this is fast and lossless)
into fragmented MP4 is by far the simplest fix -- done once per segment and
cached next to it, so repeat playback is instant.
"""
import logging
import os
import subprocess
from typing import Optional, Tuple

log = logging.getLogger("scte35_analyzer.media.segments")


def _probe_audio_codecs(ts_path: str, timeout: int) -> list:
    """codec_name of each audio stream in ts_path, in stream order -- the
    same order `-map 0:a?` below selects them in, so audio_codecs[i] lines
    up with output audio stream i. Best-effort: returns [] on any ffprobe
    failure so the caller just skips the ADTS-to-ASC fix below rather than
    failing the whole remux over a probe that itself failed.

    Deduplicated by each stream's own `index` (not just line position):
    ffprobe has been observed to list the same MPEG-TS stream twice (e.g.
    once per PMT occurrence within the analyzed window) for a short clip.
    Without deduplication that would produce one bogus extra entry here,
    which below would turn into an extra `-bsf:a:N aac_adtstoasc` targeting
    an output audio stream index that was never actually mapped -- harmless
    on ffmpeg versions that just ignore an unmatched stream specifier, but
    not guaranteed across versions, so collapse it here instead."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a",
           "-show_entries", "stream=index,codec_name", "-of", "csv=p=0", ts_path]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    by_index = {}
    for line in proc.stdout.decode(errors="replace").splitlines():
        idx_str, _, codec = line.strip().partition(",")
        if not idx_str.isdigit():
            continue
        by_index[int(idx_str)] = codec
    return [by_index[idx] for idx in sorted(by_index)]


def ensure_mp4(ts_path: str, mp4_path: str, timeout: int = 120) -> Tuple[bool, Optional[str]]:
    """Returns (True, None) if mp4_path exists (or was just created) and is
    playable-looking, or (False, error_message) on failure. Idempotent -- a
    second call for the same segment is a no-op if the file is already
    there."""
    if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
        return True, None
    os.makedirs(os.path.dirname(mp4_path), exist_ok=True)
    tmp_path = mp4_path + ".tmp"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", ts_path,
        # A raw --ts-dump-dir capture is byte-exact, every PID -- that
        # includes the SCTE-35 data PID (and, on a multi-program feed,
        # whatever else shares the multiplex). MP4 has no way to carry a
        # raw SCTE-35/private-data stream, and ffmpeg's default "-c copy"
        # stream auto-selection can still try to map an unrecognized
        # stream on some inputs and abort the whole remux over it. Map
        # exactly what a browser <video> tag can play -- the first video
        # stream, plus any audio -- and explicitly drop data/subtitles.
        "-map", "0:v:0?", "-map", "0:a?", "-dn", "-sn",
        "-c", "copy",
    ]
    # AAC audio inside an MPEG-TS is ADTS-framed (each frame carries its own
    # header); MP4 instead wants the bare AAC bitstream described once via
    # an Audio Specific Config in the sample description. Copying
    # ADTS-framed AAC straight into MP4 without translating it makes
    # ffmpeg's MP4 muxer fail outright ("Malformed AAC bitstream detected"
    # / "Error writing trailer: Operation not permitted") instead of just
    # producing a file with broken audio -- so the whole segment (video
    # included) previously failed to play at all. -bsf:a:N aac_adtstoasc
    # does that ADTS->ASC translation losslessly (still no re-encode).
    # Applied per output audio stream index, and only to streams that are
    # actually AAC -- running it on a non-AAC stream (e.g. AC-3) errors
    # the same way the unfixed bug did.
    for i, codec in enumerate(_probe_audio_codecs(ts_path, timeout)):
        if codec == "aac":
            cmd += [f"-bsf:a:{i}", "aac_adtstoasc"]
    cmd += [
        # empty_moov writes the moov (with each stream's codec config) up
        # front, before any packets -- for most codecs ffmpeg already has
        # everything it needs for that from the demuxer at open time. AC-3
        # is an exception: its moov entry needs a frame's actual bitstream
        # info (sample rate code, bsid, etc., written into the dac3 box),
        # which isn't available until the first AC-3 packet is read. With
        # plain empty_moov that fails outright ("Cannot write moov atom
        # before AC3 packets") instead of writing anything at all -- delay_moov
        # makes ffmpeg hold the moov until each stream's first packet has
        # been seen, which is a no-op for codecs that didn't need it and
        # fixes AC-3.
        "-movflags", "frag_keyframe+empty_moov+delay_moov+default_base_moof",
        "-f", "mp4", tmp_path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        msg = f"ffmpeg remux timed out after {timeout}s"
        log.warning("%s for %s", msg, ts_path)
        return False, msg
    if proc.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        stderr_tail = proc.stderr.decode(errors="replace").strip()[-500:]
        msg = f"ffmpeg exit {proc.returncode}: {stderr_tail}" if stderr_tail else f"ffmpeg exit {proc.returncode}"
        log.warning("Remux failed for %s: %s", ts_path, msg)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False, msg
    os.replace(tmp_path, mp4_path)
    return True, None
