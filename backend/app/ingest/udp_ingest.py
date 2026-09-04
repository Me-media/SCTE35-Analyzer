"""Ingest source: a live UDP feed -- real IGMP multicast, plain unicast (used
both for genuine unicast feeds and for the loopback relay ffmpeg_relay.py
sets up for HLS/DASH), raw MPEG-TS-over-UDP or RTP-encapsulated.

This is app.core.probe.main()'s live socket loop, factored out so it can be
run in a background thread per job and stopped cleanly from the GUI instead
of only ever responding to SIGINT. open_multicast_socket() already sets a
2-second recv timeout, which doubles as the stop_event poll interval here.
"""
import logging

from app.core import probe as core

log = logging.getLogger("scte35_analyzer.ingest.udp")


def run(probe: core.Probe, addr: str, port: int, iface: str | None,
        transport: str, stop_event, on_status=None):
    try:
        sock = core.open_multicast_socket(addr, port, iface)
    except SystemExit as exc:
        # open_multicast_socket()/validate_ipv4_literal() call sys.exit(1)
        # on a bad address/bind/join failure (this is a CLI tool at heart)
        # -- turn that into a normal error report instead of killing the
        # whole backend process.
        msg = f"Failed to open UDP socket for {addr}:{port}: {exc}"
        log.error(msg)
        probe.close()
        if on_status:
            on_status("error", msg)
        return

    if core.is_multicast_ipv4(addr):
        log.info("Joined multicast %s:%d (iface=%s, transport=%s)", addr, port, iface or "any", transport)
    else:
        log.info("Listening for unicast UDP on %s:%d (transport=%s)", addr, port, transport)

    if on_status:
        on_status("running", None)

    leftover = bytearray()
    transport_mode = transport
    packets_seen = 0
    try:
        while not stop_event.is_set():
            try:
                data, _addr = sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                if stop_event.is_set():
                    break
                raise

            if transport_mode == "auto":
                transport_mode = "rtp" if core.looks_like_rtp(data) else "ts"
                log.info("Auto-detected transport: %s", transport_mode)
            if transport_mode == "rtp":
                data = core.strip_rtp(data)

            leftover += data
            packets, leftover = core.extract_ts_packets(leftover)
            leftover = bytearray(leftover)
            for pkt in packets:
                packets_seen += 1
                probe.handle_ts_packet(pkt)
    except Exception as exc:  # noqa: BLE001 -- report, don't crash the worker thread silently
        log.exception("Live UDP ingest failed")
        probe.flush_pending_as_missed(reason="timeout")
        probe.close()
        sock.close()
        if on_status:
            on_status("error", str(exc))
        return

    probe.flush_pending_as_missed(reason="timeout")
    probe.close()
    sock.close()
    log.info("Stopped UDP ingest for %s:%d. %d TS packets processed.", addr, port, packets_seen)
    if on_status:
        on_status("stopped", None)
