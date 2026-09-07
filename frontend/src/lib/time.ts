/** Every timestamp the backend writes (marker/cue/segment wallclock,
 * video_info.sampled_at, segment window bounds, etc.) is a naive ISO8601
 * string -- no "Z"/offset suffix -- and is always UTC (see backend/app/db.py's
 * _now(), which is explicit about using time.gmtime() for exactly this
 * reason). The GUI labels these fields "UTC" and additionally renders a
 * local-time conversion next to them for convenience; this file is the one
 * place that conversion happens, so every field does it the same way.
 *
 * Why parseUtcWallclock exists instead of `new Date(iso)`: a timezone-less
 * ISO string is parsed as the VIEWER'S LOCAL time by the JS Date spec, not
 * UTC -- passing a UTC-but-unmarked string straight to `new Date()` would
 * silently produce the wrong instant for any viewer not in UTC themselves.
 * Appending "Z" forces the correct interpretation. */
export function parseUtcWallclock(iso: string): Date {
  return new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** The viewer's local-time rendering of a stored UTC wallclock string, for
 * display next to the raw UTC value -- GUI-only, never sent back to the API
 * and never included in an export (CSV/JSON stay UTC-only, matching what's
 * actually stored). */
export function formatLocal(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = parseUtcWallclock(iso);
  if (Number.isNaN(d.getTime())) return "–";
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
  );
}

/** HH:MM:SS, UTC -- for the live header clock (a Date "now", not a stored
 * wallclock string, so no parsing/timezone-marker issue applies here). */
export function formatUtcClock(d: Date): string {
  return `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`;
}

/** HH:MM:SS, viewer's local time -- for the live header clock. */
export function formatLocalClock(d: Date): string {
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/** IANA zone name (e.g. "Europe/Stockholm") for a tooltip on the "Local"
 * label -- so it's unambiguous which "local" is meant when this is looked
 * at on a screenshot or by someone in a different timezone. */
export function localZoneName(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    return "local";
  }
}

/** For an <input type="datetime-local">: browsers read/write that value as
 * naive LOCAL time (no timezone marker) -- unlike parseUtcWallclock above,
 * that's exactly the right interpretation here, since the value is what
 * the viewer typed on their own clock (the manual time-range picker on the
 * delta chart). */
export function epochToDatetimeLocalValue(ms: number): string {
  const d = new Date(ms);
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
  );
}

/** Inverse of epochToDatetimeLocalValue -- null if the field is empty/not
 * a valid date (e.g. mid-edit). */
export function datetimeLocalValueToEpoch(value: string): number | null {
  if (!value) return null;
  const t = new Date(value).getTime();
  return Number.isNaN(t) ? null : t;
}

/** UTC axis-tick label for an arbitrary instant, with the level of detail
 * (date included or not, seconds included or not) chosen from how wide the
 * surrounding time window is -- used by the delta chart's time axis. */
export function formatUtcAxisTick(ms: number, spanMs: number): string {
  const d = new Date(ms);
  const hm = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
  if (spanMs > 36 * 3600 * 1000) {
    return `${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ${hm}`;
  }
  if (spanMs <= 5 * 60 * 1000) {
    return `${hm}:${pad2(d.getUTCSeconds())}`;
  }
  return hm;
}
