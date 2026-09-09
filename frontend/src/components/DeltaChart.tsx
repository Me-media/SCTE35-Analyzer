import { useMemo, useState } from "react";
import type { Marker } from "../api/types";
import {
  datetimeLocalValueToEpoch,
  epochToDatetimeLocalValue,
  formatUtcAxisTick,
  parseUtcWallclock,
} from "../lib/time";
import { useContainerWidth } from "../lib/useContainerWidth";
import { verdictVariant } from "./Badge";

const DOT_COLOR: Record<string, string> = {
  ok: "#22c55e",
  warn: "#f59e0b",
  bad: "#ef4444",
  missed: "#94a3b8",
  neutral: "#38bdf8",
};

// Only the initial-render fallback, before useContainerWidth reports the
// plot's actual measured width below -- see that hook for why the viewBox
// width has to track the real rendered width instead of staying fixed
// (a fixed viewBox stretched to fill a wider container scales dot/text
// size right along with the axes, since they share the same coordinate
// space -- exactly the "chart enlarges when the window is resized" bug
// this was built to fix).
const DEFAULT_WIDTH = 900;
const HEIGHT = 220;
const PAD_LEFT = 56;
const PAD_RIGHT = 16;
const PAD_TOP = 16;
const PAD_BOTTOM = 28;
const MISSED_ROW_Y = PAD_TOP + 10;

const ZOOM_PRESETS: { label: string; seconds: number }[] = [
  { label: "1mo", seconds: 30 * 24 * 3600 },
  { label: "1w", seconds: 7 * 24 * 3600 },
  { label: "1d", seconds: 24 * 3600 },
  { label: "12h", seconds: 12 * 3600 },
  { label: "6h", seconds: 6 * 3600 },
  { label: "3h", seconds: 3 * 3600 },
  { label: "1h", seconds: 3600 },
];

type Range = { kind: "all" } | { kind: "preset"; seconds: number } | { kind: "custom"; start: number; end: number };

function pillClass(active: boolean): string {
  return `rounded px-2 py-1 text-xs ring-1 transition ${
    active ? "bg-sky-600/20 text-sky-300 ring-sky-600/40" : "text-slate-400 ring-slate-700 hover:bg-slate-800/60"
  }`;
}

/** Plain SVG scatter of delta_ms (SCTE-35 target PTS vs. matched IDR PTS)
 * per marker, plotted against real wall-clock time (UTC) on the X axis --
 * no charting library, so this stays dependency-free, matching
 * CompareChart's approach. A MISSED marker (no delta_ms) is drawn on its
 * own row at the top instead of being silently dropped from the plot.
 *
 * Owns its own zoom/time-range state (quick presets + a manual
 * from/to picker) rather than taking it as props -- JobDetail renders this
 * with `key={jobId}` so switching jobs remounts it and resets the zoom,
 * instead of carrying a stale range over onto a different channel's data. */
export default function DeltaChart({ markers, okThresholdMs }: { markers: Marker[]; okThresholdMs: number }) {
  const [chartRef, WIDTH] = useContainerWidth(DEFAULT_WIDTH);
  const [range, setRange] = useState<Range>({ kind: "all" });
  const [showCustom, setShowCustom] = useState(false);
  const [draftStart, setDraftStart] = useState("");
  const [draftEnd, setDraftEnd] = useState("");
  const [customError, setCustomError] = useState<string | null>(null);

  // Every marker with a parseable wallclock, timestamped in ms -- computed
  // once regardless of the current zoom, since presets/latest-anchor need
  // the full set, not just what's currently visible.
  const timed = useMemo(() => {
    const out: { m: Marker; t: number }[] = [];
    for (const m of markers) {
      if (!m.wallclock) continue;
      const t = parseUtcWallclock(m.wallclock).getTime();
      if (!Number.isNaN(t)) out.push({ m, t });
    }
    return out;
  }, [markers]);

  const latestMs = useMemo(() => {
    let max = -Infinity;
    for (const { t } of timed) if (t > max) max = t;
    return Number.isFinite(max) ? max : Date.now();
  }, [timed]);

  const bounds = useMemo(() => {
    if (range.kind === "all") return null; // null = auto-fit to the data's own extent
    if (range.kind === "preset") return { start: latestMs - range.seconds * 1000, end: latestMs };
    return { start: range.start, end: range.end };
  }, [range, latestMs]);

  const visible = useMemo(() => {
    if (!bounds) return timed;
    return timed.filter(({ t }) => t >= bounds.start && t <= bounds.end);
  }, [timed, bounds]);

  const { points, missed, yMin, yMax, tMin, tMax } = useMemo(() => {
    const withDelta = visible.filter(({ m }) => m.delta_ms !== null) as { m: Marker & { delta_ms: number }; t: number }[];
    const missedList = visible.filter(({ m }) => m.delta_ms === null);
    const values = withDelta.map(({ m }) => m.delta_ms);
    let lo = Math.min(0, ...values, -okThresholdMs);
    let hi = Math.max(0, ...values, okThresholdMs);
    if (!isFinite(lo)) lo = -okThresholdMs;
    if (!isFinite(hi)) hi = okThresholdMs;
    const span = hi - lo || 1;
    lo -= span * 0.1;
    hi += span * 0.1;

    let tLo: number, tHi: number;
    if (bounds) {
      tLo = bounds.start;
      tHi = bounds.end;
    } else if (visible.length > 0) {
      const ts = visible.map(({ t }) => t);
      tLo = Math.min(...ts);
      tHi = Math.max(...ts);
      if (tHi - tLo < 1000) {
        tLo -= 30_000;
        tHi += 30_000;
      }
    } else {
      tLo = latestMs - 3_600_000;
      tHi = latestMs;
    }
    return { points: withDelta, missed: missedList, yMin: lo, yMax: hi, tMin: tLo, tMax: tHi };
  }, [visible, okThresholdMs, bounds, latestMs]);

  function openCustomPicker() {
    const b = bounds ?? { start: latestMs - 3_600_000, end: latestMs };
    setDraftStart(epochToDatetimeLocalValue(b.start));
    setDraftEnd(epochToDatetimeLocalValue(b.end));
    setCustomError(null);
    setShowCustom(true);
  }

  function applyCustomRange() {
    const start = datetimeLocalValueToEpoch(draftStart);
    const end = datetimeLocalValueToEpoch(draftEnd);
    if (start === null || end === null) {
      setCustomError("Enter both a start and end time.");
      return;
    }
    if (start >= end) {
      setCustomError("Start must be before end.");
      return;
    }
    setCustomError(null);
    setRange({ kind: "custom", start, end });
  }

  if (markers.length === 0) {
    return <div className="flex h-[220px] items-center justify-center text-sm text-slate-500">No markers yet</div>;
  }

  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT;
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM - 22; // reserve a lane for missed dots
  const tSpan = tMax - tMin || 1;
  const xFor = (t: number) => PAD_LEFT + ((t - tMin) / tSpan) * plotW;
  const yFor = (v: number) => PAD_TOP + 22 + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  const zeroY = yFor(0);
  const okTopY = yFor(okThresholdMs);
  const okBotY = yFor(-okThresholdMs);

  const TICK_COUNT = 5;
  const ticks = Array.from({ length: TICK_COUNT }, (_, i) => tMin + (tSpan * i) / (TICK_COUNT - 1));

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <button onClick={() => setRange({ kind: "all" })} className={pillClass(range.kind === "all")}>
          All
        </button>
        {ZOOM_PRESETS.map((p) => (
          <button
            key={p.label}
            onClick={() => setRange({ kind: "preset", seconds: p.seconds })}
            className={pillClass(range.kind === "preset" && range.seconds === p.seconds)}
          >
            {p.label}
          </button>
        ))}
        <button
          onClick={() => (showCustom ? setShowCustom(false) : openCustomPicker())}
          className={pillClass(range.kind === "custom")}
        >
          Custom range {showCustom ? "▲" : "▼"}
        </button>
      </div>

      {showCustom && (
        <div className="mb-3 flex flex-wrap items-end gap-2 rounded-md bg-slate-950/40 p-2 ring-1 ring-slate-800">
          <label className="flex flex-col text-[11px] text-slate-400">
            From (local)
            <input
              type="datetime-local"
              step={1}
              value={draftStart}
              onChange={(e) => setDraftStart(e.target.value)}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 ring-1 ring-slate-700"
            />
          </label>
          <label className="flex flex-col text-[11px] text-slate-400">
            To (local)
            <input
              type="datetime-local"
              step={1}
              value={draftEnd}
              onChange={(e) => setDraftEnd(e.target.value)}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 ring-1 ring-slate-700"
            />
          </label>
          <button
            onClick={applyCustomRange}
            className="rounded-md bg-sky-600/20 px-2.5 py-1 text-xs text-sky-300 ring-1 ring-sky-600/40 hover:bg-sky-600/30"
          >
            Apply
          </button>
          {customError && <span className="text-xs text-red-400">{customError}</span>}
        </div>
      )}

      {/* This wrapper is what useContainerWidth measures -- kept mounted
          across both branches below (not just around the <svg>) so the
          chart's real width is already known by the time data actually
          shows up, instead of a one-frame jump from DEFAULT_WIDTH. */}
      <div ref={chartRef} className="w-full">
      {visible.length === 0 ? (
        <div className="flex h-[220px] items-center justify-center text-sm text-slate-500">
          No markers in the selected time range
        </div>
      ) : (
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="w-full h-[220px]" preserveAspectRatio="none">
          {/* OK-threshold band */}
          <rect
            x={PAD_LEFT}
            y={Math.min(okTopY, okBotY)}
            width={plotW}
            height={Math.abs(okBotY - okTopY)}
            fill="#22c55e"
            opacity={0.08}
          />
          {/* zero line */}
          <line x1={PAD_LEFT} x2={WIDTH - PAD_RIGHT} y1={zeroY} y2={zeroY} stroke="#475569" strokeWidth={1} />
          {/* axis */}
          <line x1={PAD_LEFT} x2={PAD_LEFT} y1={PAD_TOP} y2={HEIGHT - PAD_BOTTOM} stroke="#334155" strokeWidth={1} />
          <line
            x1={PAD_LEFT}
            x2={WIDTH - PAD_RIGHT}
            y1={HEIGHT - PAD_BOTTOM}
            y2={HEIGHT - PAD_BOTTOM}
            stroke="#334155"
            strokeWidth={1}
          />
          <text x={4} y={zeroY + 4} fontSize={10} fill="#94a3b8">
            0ms
          </text>
          <text x={4} y={PAD_TOP + 22 + 8} fontSize={10} fill="#94a3b8">
            {yMax.toFixed(0)}
          </text>
          <text x={4} y={HEIGHT - PAD_BOTTOM} fontSize={10} fill="#94a3b8">
            {yMin.toFixed(0)}
          </text>
          <text x={4} y={MISSED_ROW_Y + 3} fontSize={9} fill="#64748b">
            MISSED
          </text>

          {ticks.map((t, i) => (
            <text
              key={i}
              x={Math.min(Math.max(xFor(t), PAD_LEFT), WIDTH - PAD_RIGHT - 4)}
              y={HEIGHT - PAD_BOTTOM + 14}
              fontSize={9.5}
              fill="#64748b"
              textAnchor={i === 0 ? "start" : i === TICK_COUNT - 1 ? "end" : "middle"}
            >
              {formatUtcAxisTick(t, tSpan)}
            </text>
          ))}

          {missed.map(({ m, t }) => (
            <circle key={`missed-${m.id}`} cx={xFor(t)} cy={MISSED_ROW_Y} r={3.5} fill={DOT_COLOR.missed}>
              <title>{`event_id=${m.event_id ?? "n/a"} verdict=${m.verdict} wallclock=${m.wallclock ?? ""}`}</title>
            </circle>
          ))}

          {points.map(({ m, t }) => (
            <circle key={m.id} cx={xFor(t)} cy={yFor(m.delta_ms)} r={3.5} fill={DOT_COLOR[verdictVariant(m.verdict)]}>
              <title>
                {`event_id=${m.event_id ?? "n/a"} delta=${m.delta_ms.toFixed(1)}ms verdict=${m.verdict} wallclock=${m.wallclock ?? ""}`}
              </title>
            </circle>
          ))}
        </svg>
      )}
      </div>
      <p className="mt-1 text-[10px] text-slate-500">Times shown on the axis are UTC.</p>
    </div>
  );
}
