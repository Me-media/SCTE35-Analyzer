![SCTE35 Analyzer](docs/logo.svg)

# SCTE35 Analyzer

A web-based, Docker-packaged successor to [`scte35diff`](https://github.com/Me-media/scte35diff)
(`scte35_idr_diff.py`) — the same measurement engine (SCTE-35 splice point
vs. video-IDR PTS delta), but as a running service with a modern GUI
instead of a CLI script you run once per stream.

Measures, live or against a saved file, the deviation in milliseconds
between the PTS an SCTE-35 splice message (`splice_insert`/`time_signal`)
points to and the nearest video IDR — this is the value that determines
whether a downstream splicer/switch can make a clean transition on ad
insertion. See the original project's README (linked above) for the full
technical background on the measurement itself; this document focuses on
the GUI/operations side.

Current version: **0.11.0** — shown in the bottom-right corner of the GUI
and via `GET /api/version` (a quick way to confirm a deploy actually
picked up new code). See [CHANGELOG.md](./CHANGELOG.md) for the full
version history.

## Features

- **Four ways to feed in a stream:** upload an MPEG-TS file, connect to a
  multicast/unicast UDP stream (raw TS or RTP-encapsulated), or point at a
  **live** HLS (`.m3u8`) or DASH (`.mpd`) URL (relayed via `ffmpeg -c
  copy`, no re-encode).
- **Markers in real time**, both as a table (every field: event_id, type,
  target PTS, IDR PTS, delta, verdict, pre-roll, signaling) and graphically
  (a delta chart plotted against real UTC time, color-coded by verdict),
  pushed to the browser over WebSocket as the engine finds them. The chart
  zooms to quick presets (1mo/1w/1d/12h/6h/3h/1h) anchored to the most
  recent marker, or a manually picked from/to time range.
- **`gop_verdict`** — a per-marker diagnostic for "why do ads start later
  than they should": compares `delta_ms` against the stream's own sampled
  GOP duration to flag whether the encoder likely forced a real keyframe
  at the splice point (`FORCED`) or just let the packager fall through to
  its next naturally-scheduled IDR instead (`GOP_WAIT` — the actual root
  cause to chase, since a downstream splicer can only cut on an IDR that
  exists). Shown as its own column/badge next to Verdict in the marker
  table and the multi-stream comparison view, and included in CSV/JSON
  exports. Requires a GOP sample to have landed (`video_info`, on by
  default, refreshed every ~20s) — reported as `N/A` until then.
- **JPEG snapshot** of the matched IDR frame per marker (and optionally N
  frames before it, to see the actual transition) — see immediately
  whether the cut was clean, black, or corrupted instead of trusting the
  PTS math alone.
- **Reference-frame snapshots** for `time_to_event_ms` and the pre-roll
  deadline — see exactly which video frame those numbers refer to, not
  just the millisecond figure. Each is independently selectable per job:
  - *Time to event*: **frame on air when the cue arrived** (what was
    actually playing the instant the SCTE-35 message was registered), or
    **frame at the literal target PTS** (the frame the cue's own target
    PTS points to, captured once it's actually observed).
  - *Pre-roll*: **frame at the real-time deadline** (whatever is on air at
    the wall-clock moment the declared `time_to_event_ms` elapses), or
    **same frame as the matched IDR snapshot** (just relabels that
    snapshot — requires the matched-IDR snapshot to be enabled too).
- **Clip saving:** the same "save raw TS around SCTE-35 events" feature as
  the original tool (`--ts-dump-dir`), but the GUI plays the clips back
  directly in the browser (on-demand remux to fragmented MP4 via `ffmpeg
  -c copy`, no quality loss) instead of you having to download and open
  the files manually.
- **CSV export** per job, same column schema as the CLI tool's `--csv-out`.
- Every measurement parameter (tolerances, PID overrides, codec,
  snapshot/clip settings) is adjustable per job in the GUI — mirrors the
  CLI flags in the original project.
- **Restart and edit jobs:** re-run a stopped/finished/errored job against
  its existing source without losing anything it already collected, or
  edit its name/tuning/source in place before doing so.
- **Multi-core:** each running job is its own OS process, so several jobs
  (e.g. watching several channels at once) run truly in parallel across
  CPU cores instead of contending for one.
- **Job sidebar and multi-stream comparison:** a persistent, filterable
  job list on the left for quick switching, and a live side-by-side
  comparison view (tick 2+ jobs) with a shared-time-axis delta chart and
  a merged marker table — for checking a primary/backup pair stay in
  sync, or comparing the effect of a tuning change.
- **Detailed video stream info:** pixel format, color space, color range,
  frame rate, scan type (progressive/interlaced), codec profile/level,
  resolution, and an approximate GOP structure — sampled periodically
  (every ~20s) via a local `ffprobe` pass over recently seen raw packets,
  with a manual "Refresh" button on the job detail page for an immediate
  re-check instead of waiting.
- **Storage cleanup, per job:** set an auto-delete-after limit for saved
  segments (TS dumps) and snapshots (images) — Never (default), 1h, 6h,
  12h, 1 day, or 1 week — enforced by a periodic background sweep that
  covers stopped jobs too; or run an immediate "Clean up now" one-off
  purge at a chosen age, independent of that setting. Both live behind the
  "Clean up" button next to "Export CSV" on the job detail page. The same
  menu also has "Flush all data" — deletes every marker, cue, segment and
  snapshot for that channel while keeping the channel and its settings
  (name, source, tuning, retention), for re-baselining without recreating
  the job.
- **All timestamps are UTC**, labeled as such everywhere they're shown
  (marker/segment times, the video-info sample time) — this is what's
  stored and what's in CSV exports. The marker table additionally shows a
  GUI-only "Local time" column (never stored, never exported) next to it,
  and a live UTC + local clock sits in the top-right header for a
  quick reference either way.

## Architecture

```
┌─────────────────────────────┐
│  React/Vite GUI (static)    │  ← served by the same FastAPI process
├─────────────────────────────┤
│  FastAPI: REST + WebSocket  │
├─────────────────────────────┤
│  JobManager (one OS process/│
│  job -- see below)          │
│  ├─ file_ingest             │  uploaded .ts file
│  ├─ udp_ingest              │  multicast/unicast UDP, raw TS or RTP
│  └─ ffmpeg_relay             │  HLS/DASH → local UDP (ffmpeg -c copy)
├─────────────────────────────┤
│  app/core/probe.py          │  ← the engine: a fork of scte35_idr_diff.py
│  (PAT/PMT, PES/NAL, SCTE-35 │    with an on_event() hook added; all
│   matching – unchanged      │    demux/matching logic is unchanged,
│   logic, see tests below)   │    verified against the upstream test suite
├─────────────────────────────┤
│  SQLite (jobs/markers/      │
│  cues/clips) + filesystem   │
│  (snapshots, .ts clips)     │
└─────────────────────────────┘
```

`app/core/probe.py` is a deliberately minimal fork: the entire TS/PSI/PES
demuxer, NAL scanning, and SCTE-35 matching logic is **unchanged** code
from the original project. The only addition is an `on_event(kind, data)`
hook, called at exactly the same points the CLI tool already writes to
`--csv-out`/`--json-out`/`--scte35-out`/snapshots/`--ts-dump-dir` — that's
the hook the job manager listens on to populate the database and push live
updates to the browser. `tests/test_engine_offline.py` is an unmodified
copy of the original project's own test suite, run against this fork to
verify the rewrite changed no behavior; `tests/test_event_hooks.py` and
`tests/test_integration_jobs.py` are new tests for the GUI additions
themselves (the event hook, and JobManager/DB/ingest end-to-end).

**Each running job is its own OS process**, not just a thread. Python's
GIL means only one thread's bytecode runs at a time no matter how many
CPU cores the host has, so a thread-per-job model (the original design)
left every job fighting over a single core for the CPU-bound part of this
work (the pure-Python packet demux/NAL scanning in `app/core/probe.py`).
Running each job as a `multiprocessing.Process` instead lets the OS
schedule N concurrently-running jobs onto N separate cores. Each job
process opens its own sqlite connection (fork-safe; see `app/db.py`'s
`busy_timeout`) and reports events back to the FastAPI process over a
`multiprocessing.Queue`, which a small per-job thread there forwards to
WebSocket subscribers exactly as before — the live-UI/WebSocket path
itself is unchanged. The practical implication: a running job now costs
one OS process (a few tens of MB), not just a thread, so very large job
counts use more memory than before, though far fewer jobs than would
saturate any real server's core count.

**Restarting and editing jobs:** a stopped/finished/errored job can be
restarted (Restart button) — this re-attaches to the same live source or
reprocesses the same uploaded file from the start, and *keeps* everything
already collected: new markers/cues/snapshots/clips are appended to the
job's existing history, never cleared. A non-running job's name, tuning,
and (for live sources) source address/URL can also be edited in place
(Edit button); a "file" job's uploaded file itself can't be swapped this
way — create a new job for a different file.

## Installation

Requires Docker + Docker Compose. No other local install is needed —
Python dependencies (including `threefive3` for SCTE-35 decoding) and
`ffmpeg` are installed inside the container at build time.

```bash
git clone https://github.com/Me-media/DAI-verifier-GUI.git
cd DAI-verifier-GUI
docker compose up -d --build
```

The GUI is then reachable at `http://<server-ip>:8000` (see the networking
note below for why the port isn't mapped via `-p` the usual way).

### Updating

```bash
cd DAI-verifier-GUI
git pull
docker compose up -d --build
```

The database, uploaded files, snapshots, and saved clips live in `./data/`
(bind-mounted, see `docker-compose.yml`) and survive both `git pull` and
`docker compose up --build`.

> **One-time step when updating to v0.7.0 or later:** this release renamed
> the project (again) to **SCTE35 Analyzer**, which also renamed the
> Docker service/container (`dai-verifier-gui` → `scte35-analyzer`).
> Compose tracks a running container by that name, so before your first
> `docker compose up -d --build` on this version, stop the old one first —
> `docker compose down` (run against the *old* `docker-compose.yml`, i.e.
> before `git pull`) or `docker rm -f dai-verifier-gui` — otherwise, with
> `network_mode: host` (see below), the new container would try to bind
> the same port while the old one is still holding it and just crash-loop.
> `./data/` and its sqlite database are unaffected either way (the app
> renames its own database file on its own first start); this is purely
> about the container name Compose tracks.

### Uninstalling

```bash
docker compose down
# ./data/ is NOT deleted automatically -- remove it manually to wipe everything:
# rm -rf ./data
```

## Important: networking and multicast

`docker-compose.yml` runs the container with `network_mode: host` by
default. That's a deliberate choice, not a shortcut: real IGMP multicast
join (the `--addr 239.x.x.x` live-ingest path) does **not** work reliably
through Docker's default bridge network on Linux — the bridge simply
doesn't forward external multicast traffic into the container without
extra host-side routing (`igmpproxy` or similar). With host networking the
container joins multicast groups exactly like a process running directly
on the host, and the GUI is reachable at `http://<host-ip>:8000` directly
(no `-p` mapping is needed or used).

If you **only** ever use file upload / HLS / DASH ingest (never live
multicast UDP/RTP), you can switch to normal bridge networking instead for
better isolation — see the comments in `docker-compose.yml` (comment out
`network_mode: host`, uncomment the `ports:` block).

## HLS/DASH ingest

HLS/DASH is fed in via an `ffmpeg -c copy` relay to a local UDP listener —
no re-encode, so it's effectively free CPU-wise. This is intended for
**LIVE** monitoring: `-re` paces the relay's output in real time so that
`actual_preroll_ms`/`preroll_verdict` stay meaningful. For a VOD asset
(pre-recorded content), download it and use file upload instead — otherwise
it's read as fast as the network allows, which makes the real-time-
dependent fields misleading (the same limitation as the CLI tool's
`--input-file` mode, see its README).

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SCTE35_ANALYZER_STORAGE_DIR` | `/data` | Root for the database, uploads, snapshots, clips |
| `SCTE35_ANALYZER_MAX_UPLOAD_BYTES` | 20 GiB | Upper bound on file upload size |
| `SCTE35_ANALYZER_LOG_LEVEL` | `INFO` | Python log level |

## Development (without Docker)

Backend:

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Frontend (in a second terminal — Vite's dev server proxies `/api` and
`/ws` to `:8000`, see `frontend/vite.config.ts`):

```bash
cd frontend
npm install
npm run dev
```

Running the tests:

```bash
cd backend
pip install threefive3 --break-system-packages   # needed by handle_scte35() at runtime, not by the tests themselves
python3 tests/test_engine_offline.py
python3 tests/test_event_hooks.py
python3 tests/test_integration_jobs.py
```

(None of the three test files require `threefive3` to be installed — they
stub a minimal fake `Cue` object exactly like the original project's own
test suite does, so the whole demux/matching chain can be verified without
that external dependency.)

## Known limitations

- **In-memory job state:** if the backend process restarts while a job is
  running, that job is marked `stopped` on the next startup (it is not
  automatically resumed) — its history (markers, snapshots, clips) is
  kept, and it can be resumed manually with the Restart button.
- **MPEG-2 video** isn't classified at the frame level (same limitation as
  the original tool) — PID/codec are detected, but IDR matching only
  happens for H.264/HEVC.
- This is a **diagnostic/QC tool, not a certified SCTE-35 conformance
  tester** — see the original project's README for the full set of
  caveats around `pts_adjustment`, the source of `signal_verdict`,
  `time_to_event_ms`/`actual_preroll_ms`, etc., which apply identically
  here since the matching logic is unchanged.

## License

MIT, in line with the original [`scte35diff`](https://github.com/Me-media/scte35diff)
project (see `LICENSE`). `threefive3` (MIT) is used unmodified for SCTE-35
decoding.
