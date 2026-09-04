# Changelog

All notable changes to SCTE35 Analyzer are documented here. Versioning
follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`)
loosely: MINOR for new features, PATCH for fixes-only, while the project
stays pre-1.0 and things can still move. The current version is shown in
the bottom-right corner of the GUI and via `GET /api/version` — check it
after a deploy to confirm the new code actually landed, and compare
against this file.

## [0.8.1] - 2026-09-04

### Changed
- **Moved storage cleanup into a "Clean up" button/menu next to "Export
  CSV"**, instead of its own always-visible section on the job detail
  page. Same functionality, less permanent screen real estate: the menu
  has "Segments (TS dump)" and "Snapshots (images)" as expandable
  submenu rows, each still exposing both the auto-delete-after setting
  and the one-off "Clean up now" purge introduced in 0.8.0.

## [0.8.0] - 2026-09-04 - Initial public release

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

## pre[0.8.0] - 2026-09-01

### Changed
- Development releases

- CSV export.
- Per-job adjustable tuning parameters mirroring the CLI tool's flags.
