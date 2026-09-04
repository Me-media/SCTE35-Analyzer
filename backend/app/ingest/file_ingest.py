"""Ingest source: a previously uploaded local MPEG-TS file.

This mirrors app.core.probe.process_ts_file() closely (same 1 MiB chunked
read, same extract_ts_packets() resync-on-0x47 logic) but checks a
threading.Event between chunks so a job can be stopped from the GUI mid-file
instead of only ever running to completion.
"""
import logging
import os

from app.core import probe as core

log = logging.getLogger("scte35_analyzer.ingest.file")


def run(probe: core.Probe, path: str, stop_event, on_status=None):
    if on_status:
        on_status("running", None)
    packets_seen = 0
    leftover = bytearray()
    try:
        with open(path, "rb") as f:
            while not stop_event.is_set():
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                leftover += chunk
                packets, leftover = core.extract_ts_packets(leftover)
                leftover = bytearray(leftover)
                for pkt in packets:
                    packets_seen += 1
                    probe.handle_ts_packet(pkt)
        if leftover:
            log.warning("%d trailing byte(s) at end of %s did not form a complete "
                        "188-byte TS packet -- ignored.", len(leftover), path)
    except OSError as exc:
        log.exception("Failed reading %s", path)
        probe.flush_pending_as_missed(reason="eof")
        probe.close()
        if on_status:
            on_status("error", str(exc))
        return

    probe.flush_pending_as_missed(reason="eof" if not stop_event.is_set() else "timeout")
    probe.close()
    log.info("File ingest finished: %s (%d TS packets, stopped=%s)",
              path, packets_seen, stop_event.is_set())
    if on_status:
        on_status("stopped" if stop_event.is_set() else "finished", None)
