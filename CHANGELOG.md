# Changelog

All notable changes to SCTE35 Analyzer are documented here. Versioning
follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`)
loosely: MINOR for new features, PATCH for fixes-only, while the project
stays pre-1.0 and things can still move. The current version is shown in
the bottom-right corner of the GUI and via `GET /api/version` — check it
after a deploy to confirm the new code actually landed, and compare
against this file.

## [0.10.0] - 2026-09-07

### Added
- **"Flush all data"** in the job detail page's "Clean up" menu: deletes
  every marker, cue, segment and snapshot for that channel — both the DB
  rows and their on-disk files — while leaving the channel itself and
  every one of its settings (name, source, tuning, retention) completely
  untouched. New `POST /api/jobs/{id}/flush` endpoint. Distinct from
  deleting the job entirely, and distinct from the age-based cleanup
  above it in the same menu — this is an unconditional, deliberate wipe
  for re-baselining a channel (e.g. after fixing a bad encoder config)
  without having to recreate it and lose its configuration. Confirmed
  with a browser dialog before it runs; works regardless of whether the
  channel is currently running.
- **The delta chart now plots against real UTC time** instead of arrival
  order, with zoom: quick presets (1mo/1w/1d/12h/6h/3h/1h, anchored to the
  most recent marker) or a manual from/to time range (entered in your
  local time, converted under the hood). Resets to "All" whenever you
  switch to a different job.

## [0.9.0] - 2026-09-07

### Added
- **Local-time display alongside every UTC timestamp**, and a live UTC +
  local clock in the header, next to "+ New job". GUI-only in both
  cases — nothing new is stored, and CSV/JSON exports are unchanged
  (still UTC, matching what's actually in the database).
  - The marker table's "Time" column is now labeled "Time (UTC)", with a
    new adjacent "Local time" column converting it for the viewer.
  - The video-info sample time and a saved clip's start/end are now also
    explicitly labeled "UTC" where shown.

### Fixed
- **Every wallclock timestamp the backend writes is now guaranteed UTC**,
  regardless of the container's configured timezone. Previously, `_now()`
  (app/db.py), the SCTE-35 probe's per-cue/segment/video-info timestamps
  (app/core/probe.py), and the cleanup sweep's age cutoff
  (app/jobs/cleanup.py) all called `time.strftime(...)`/`time.localtime()`
  with no explicit UTC — which happened to read as UTC on stock Docker
  images (default `TZ=UTC`), but was never actually guaranteed, and would
  have silently mismatched the new "Time (UTC)" label above the moment
  anyone set `TZ` on the container or bind-mounted `/etc/localtime` from
  the host. All of these now explicitly use `time.gmtime()`. No visible
  change if your deployment was already effectively UTC (the normal
  case); a real behavior change if it wasn't.

## [0.8.1] - 2026-09-04

### Changed
- **Moved storage cleanup into a "Clean up" button/menu next to "Export
  CSV"**, instead of its own always-visible section on the job detail
  page. Same functionality, less permanent screen real estate: the menu
  has "Segments (TS dump)" and "Snapshots (images)" as expandable
  submenu rows, each still exposing both the auto-delete-after setting
  and the one-off "Clean up now" purge introduced in 0.8.0.

## [0.8.0] - 2026-09-04

### Added
- **Storage cleanup panel on the job detail page.** Two independent
  mechanisms, both scoped per job:
  - **Auto-delete (retention):** set how long saved segments (TS dumps)
    and snapshots (images) are kept — Never (default, unchanged behavior),
    1 hour, 6 hours, 12 hours, 1 day, or 1 week — via
    `PATCH /api/jobs/{id}/retention`. Enforced by a background sweep that
    runs roughly every 15 minutes across *all* jobs regardless of whether
    they're currently running (a stopped/finished job's ingest process no
    longer exists to prune its own data, so this has to live in the
    parent process). Changing it does not require stopping the job —
    retention is a housekeeping concern that never touches the running
    probe.
  - **Clean up now:** an immediate, one-off purge (pick an age, e.g.
    "older than 1 day", or "Everything") via
    `POST /api/jobs/{id}/cleanup`, independent of the saved retention
    setting above — for a one-time cleanup without committing to an
    ongoing policy.
  - Deleting old snapshots leaves older marker rows without a thumbnail
    (the marker data itself is unaffected) — a known, acceptable
    trade-off of freeing that disk space.
  - Existing jobs default to "Never" on both categories (new
    `segment_retention_s`/`snapshot_retention_s` columns, migrated
    automatically) — nothing starts auto-deleting data just because you
    upgraded.

## [0.7.0] - 2026-09-01

### Changed
- **Renamed the project (again) to SCTE35 Analyzer** — its third name
  (`scte35-gui` → `DAI Verifier GUI` → **SCTE35 Analyzer**, see the 0.2.1
  entry below for the first rename). Covers the GUI title, page title/
  favicon, README/docs, the FastAPI app title, and internal naming:
  environment variables (`DAI_VERIFIER_GUI_*` → `SCTE35_ANALYZER_*`), the
  sqlite filename, and the Docker service/image/container name
  (`dai-verifier-gui` → `scte35-analyzer`).
  - **Deploy note:** an existing installation's sqlite database is
    renamed automatically, in place, the first time this version starts
    (`dai_verifier_gui.db` → `scte35_analyzer.db`, WAL sidecar files
    included) — no data is lost, nothing to do by hand for that part.
  - **Action required once, at the Docker level:** because Compose
    tracks a running container by its service name and this container
    now runs with a different one, stop the old container *before*
    deploying this version — `docker compose down` (run against the
    *old* `docker-compose.yml`, i.e. before `git pull`) or `docker rm -f
    dai-verifier-gui` — otherwise, with `network_mode: host`, the new
    container would try to bind the same port while the old one still
    holds it and crash-loop. See the README's Updating section.
  - If you renamed your environment's `DAI_VERIFIER_GUI_*` variables in
    your own deployment scripts/orchestration (outside this repo's
    `docker-compose.yml`, which is already updated), rename those too.

### Added
- A proper logo/mark (`docs/logo.svg`), shown in the README, the browser
  tab (favicon), and the GUI header.

## [0.6.0] - 2026-09-01

### Added
- **Detailed video stream info**: pixel format, color space (primaries/
  transfer/matrix), color range, frame rate, scan type (progressive vs.
  interlaced, with the raw field order), codec profile/level, resolution,
  and an approximate GOP structure (average I-frame spacing and a sample
  frame-type pattern like `IBBPBBPBBPBB`) -- shown in a new "Video info"
  panel on the job detail page. This is separate from the existing PID/
  codec summary (which comes from this tool's own PAT/PMT parsing and is
  known from the first packet): the new detail comes from periodically
  (every ~20s while a job is running) sampling a short window of recently
  seen raw TS packets and running `ffprobe` against it locally -- reusing
  ffprobe rather than hand-parsing H.264/HEVC SPS/VUI bitstream data
  ourselves, consistent with how clip playback remuxing already depends on
  ffmpeg/ffprobe. A "Refresh" button on the panel can trigger an immediate
  re-sample instead of waiting for the next periodic one.

### Changed
- Each job's ingest process now also runs a small background thread for
  the video-info sampling above, plus (new) a second, parent-to-child
  command channel alongside the existing event channel, used only to
  carry the manual "Refresh" request through to the right job's process.
  No effect on jobs that never open the Video info panel.

### Added
- **Persistent job sidebar** on the left of every view, listing all jobs
  (with a live status dot and a quick filter box) so you can switch
  straight from one job to another without going back to the jobs list.
- **Compare two or more jobs** side by side, live: tick jobs in the
  sidebar and hit "Compare" for per-job summary cards (marker counts,
  OK/warn/bad/missed, average |delta|), a combined delta chart plotted on
  a shared real-world time axis and colored by stream (click a legend
  entry to hide/show a stream), and a merged, time-ordered marker table
  across all compared jobs. Useful for e.g. checking a primary and a
  redundant encoder are behaving the same, or comparing tuning changes on
  the same source.

## [0.4.2] - 2026-09-01

### Fixed
- Clip playback failed for a clip with AC-3 audio ("Cannot write moov
  atom before AC3 packets. Set the delay_moov flag to fix this." /
  "Could not write header (incorrect codec parameters ?): Invalid
  argument", ffmpeg exit 234). `-movflags empty_moov` writes the moov box
  (which for AC-3 must include bitstream info only available from an
  actual AC-3 frame) before any packets are read -- fine for codecs that
  don't need frame data for their moov entry, fatal for AC-3. Added
  `delay_moov` to the remux's movflags, which holds the moov until each
  stream's first packet is seen; no effect on codecs that didn't need it.

## [0.4.1] - 2026-09-01

### Fixed
- Clip playback failed outright ("Malformed AAC bitstream detected" /
  "Error writing trailer: Operation not permitted") for any saved clip
  whose audio was AAC. AAC inside an MPEG-TS is ADTS-framed; the on-demand
  TS→MP4 remux now runs the `aac_adtstoasc` bitstream filter (per output
  audio stream, only when that stream is actually AAC) to translate it to
  the bare bitstream MP4 requires, instead of copying it straight through
  and having ffmpeg's MP4 muxer abort the entire mux over it.

### Changed
- The version number shown in the GUI moved from next to the header title
  to a small badge in the bottom-right corner.

## [0.4.0] - 2026-09-01

### Added
- **Restart a job.** A stopped/finished/errored job can be re-run against
  its existing source (re-attach to the same live UDP/HLS/DASH source, or
  reprocess the same uploaded file from the start) via a new "Restart"
  button, shown wherever "Stop" would normally be. Everything the job
  already collected is kept — new markers/cues/snapshots/clips are
  appended to its history, never cleared.
- **Edit a job.** A non-running job's name, tuning parameters, and (for
  live sources) address/URL can be changed in place via a new "Edit"
  button, reusing the job-creation form pre-filled with the job's current
  values. A "file" job's uploaded file itself can't be swapped this way —
  create a new job for a different file.
- `GET /api/version` and a version number shown in the GUI header, so a
  deploy can be confirmed at a glance instead of guessing whether the
  browser or container picked up new code.

### Changed
- **Each running job now runs as its own OS process instead of a
  thread**, so multiple jobs running at once (e.g. watching several
  channels) genuinely use multiple CPU cores instead of contending for
  one under Python's GIL. No behavior change for a single job; a running
  job now costs one OS process rather than just a thread.
- Restarting a job now also clears any stale error message left over
  from a previous failed run.

## [0.3.0] - 2026-08-31

### Fixed
- Deleting a job that had ever captured a time-to-event/pre-roll
  reference snapshot failed with a foreign-key violation (`cue_snapshots`
  wasn't cleared by `delete_job()`), silently leaving the job undeleted
  with no error shown in the GUI.
- Video clip playback could 500 when the raw `--ts-dump`-style capture
  contained the SCTE-35 data PID alongside the video PID — ffmpeg's
  default stream mapping choked on the unmappable data stream during
  remux.

### Added
- Click-to-enlarge lightbox for IDR/reference snapshot thumbnails.

## [0.2.2] - 2026-08-31

### Fixed
- Live `match_result` WebSocket events were missing `id`/`job_id`
  (present only after a WebSocket-backlog replay from the DB), which
  broke marker-list de-duplication and pre-frame snapshot image URLs in
  the GUI for markers seen live.

## [0.2.1] - 2026-08-31

### Changed
- Renamed the project from `scte35-gui` to **DAI Verifier GUI** to match
  its actual GitHub repository name — updated throughout the code,
  Docker config, and docs.

## [0.2.0] - 2026-08-28

### Added
- Reference-frame snapshots for `time_to_event_ms` and the pre-roll
  deadline (frame on air when the cue arrived / at the literal target
  PTS / at the real-time pre-roll deadline / same as the matched IDR
  snapshot), independently selectable per job.

### Changed
- Translated the entire GUI and README from Swedish to English.

## [0.1.0] - 2026-08-28

### Added
- Initial release: SCTE-35 marker analysis GUI (a GUI successor to the
  `scte35_idr_diff.py` CLI tool) — FastAPI backend + React frontend,
  Docker-based deployment.
- Four ingest sources: uploaded MPEG-TS file, multicast/unicast UDP (raw
  TS or RTP), live HLS, live DASH.
- Real-time marker table and delta chart over WebSocket.
- JPEG snapshot of the matched IDR frame, optionally N frames before it.
- Clip saving (raw TS around SCTE-35 events) with in-browser playback.
- CSV export.
- Per-job adjustable tuning parameters mirroring the CLI tool's flags.
