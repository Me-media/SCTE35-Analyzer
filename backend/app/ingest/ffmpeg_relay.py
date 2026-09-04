"""Ingest source: HLS or DASH, via an ffmpeg subprocess relay.

app.core.probe's demuxer only understands raw MPEG-TS-over-UDP (or
RTP-encapsulated). HLS/DASH are adaptive-bitrate, segmented, HTTP-fetched
formats with their own container/manifest logic that the probe knows
nothing about -- reimplementing an HLS/DASH client inside the probe would
duplicate what ffmpeg already does correctly.

Instead: spawn `ffmpeg -i <url> -c copy -f mpegts udp://127.0.0.1:<port>`,
which remuxes (NOT re-encodes -- `-c copy`, so no quality loss and trivial
CPU cost) whichever rendition it selects into plain MPEG-TS-over-UDP on
loopback, then hand that off to udp_ingest.run() exactly like a real
unicast feed -- 127.0.0.1 is already treated as plain unicast by
open_multicast_socket() (see the README's "why 127.0.0.1" note), so no
multicast join is attempted for this loopback hop.

`-re` paces ffmpeg's *output* at the stream's native frame rate, so a live
HLS/DASH source behaves like a genuine real-time feed for the purposes of
actual_preroll_ms/preroll_verdict -- without it, a source ffmpeg can read
faster than real time (e.g. a VOD asset, or a live edge with a deep DVR
window) would be drained as fast as disk/network I/O allows, which quietly
invalidates those two fields the same way --input-file file mode does (see
the original project's README). This tool's HLS/DASH ingest is intended for
LIVE monitoring; a VOD asset is better analyzed by downloading it and using
the file-upload ingest path instead.
"""
import logging
import socket
import subprocess
import threading

from app.ingest import udp_ingest

log = logging.getLogger("scte35_analyzer.ingest.ffmpeg_relay")


def _pick_free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run(probe, url: str, kind: str, stop_event, on_status=None):
    """kind is "hls" or "dash" -- purely for logging/diagnostics, since
    ffmpeg's own input demuxer auto-detection handles both identically from
    here (an .m3u8 URL is read by the hls demuxer, an .mpd URL by dash)."""
    port = _pick_free_udp_port()
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re",
        "-i", url,
        "-c", "copy",
        "-f", "mpegts",
        f"udp://127.0.0.1:{port}?pkt_size=1316",
    ]
    log.info("Starting ffmpeg %s relay: %s -> udp://127.0.0.1:%d", kind, url, port)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True)

    stderr_lines: list[str] = []

    def _drain_stderr():
        if not proc.stderr:
            return
        for line in proc.stderr:
            stderr_lines.append(line.rstrip())
            if len(stderr_lines) > 200:
                stderr_lines.pop(0)
            log.debug("ffmpeg[%s]: %s", kind, line.rstrip())

    drain_thread = threading.Thread(target=_drain_stderr, daemon=True)
    drain_thread.start()

    def _watch_ffmpeg_exit():
        rc = proc.wait()
        if rc != 0 and not stop_event.is_set():
            tail = "\n".join(stderr_lines[-20:])
            log.error("ffmpeg %s relay for %s exited with code %d:\n%s", kind, url, rc, tail)
            if on_status:
                on_status("error", f"ffmpeg exited with code {rc}: {tail[-500:]}")
            stop_event.set()  # unblock udp_ingest.run()'s loop below

    watch_thread = threading.Thread(target=_watch_ffmpeg_exit, daemon=True)
    watch_thread.start()

    try:
        udp_ingest.run(probe, "127.0.0.1", port, None, "ts", stop_event, on_status=on_status)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        drain_thread.join(timeout=2)
        watch_thread.join(timeout=2)
