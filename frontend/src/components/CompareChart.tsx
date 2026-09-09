import { useMemo } from "react";
import type { Marker } from "../api/types";
import { useContainerWidth } from "../lib/useContainerWidth";

export interface CompareSeries {
  jobId: string;
  jobName: string;
  color: string;
  markers: Marker[];
}

// Only the initial-render fallback -- see useContainerWidth for why the
// viewBox width has to track the container's actual measured width
// (matches the same fix in DeltaChart.tsx).
const DEFAULT_WIDTH = 900;
const HEIGHT = 260;
const PAD_LEFT = 56;
const PAD_RIGHT = 16;
const PAD_TOP = 16;
const PAD_BOTTOM = 30;

/** Multi-stream delta_ms scatter, plain SVG (no charting library, matches
 * DeltaChart's approach). Unlike DeltaChart -- which plots one job's own
 * markers in arrival order -- this plots several jobs' markers against a
 * shared real-world time axis (wallclock), which is the whole point of
 * comparing streams: whether two feeds are behaving the same at the same
 * moment, not just whether each looks fine in isolation. Colored per job
 * instead of per verdict; click a legend entry to hide/show that job. */
export default function CompareChart({
  series,
  hidden,
  onToggle,
}: {
  series: CompareSeries[];
  hidden: Set<string>;
  onToggle: (jobId: string) => void;
}) {
  const [chartRef, WIDTH] = useContainerWidth(DEFAULT_WIDTH);
  const { points, tMin, tMax, yMin, yMax } = useMemo(() => {
    const pts: { jobId: string; jobName: string; color: string; t: number; delta: number; marker: Marker }[] = [];
    for (const s of series) {
      if (hidden.has(s.jobId)) continue;
      for (const m of s.markers) {
        if (m.delta_ms === null || !m.wallclock) continue;
        const t = Date.parse(m.wallclock);
        if (Number.isNaN(t)) continue;
        pts.push({ jobId: s.jobId, jobName: s.jobName, color: s.color, t, delta: m.delta_ms, marker: m });
      }
    }
    if (pts.length === 0) {
      return { points: pts, tMin: 0, tMax: 1, yMin: -1, yMax: 1 };
    }
    const ts = pts.map((p) => p.t);
    const deltas = pts.map((p) => p.delta);
    let lo = Math.min(0, ...deltas);
    let hi = Math.max(0, ...deltas);
    const span = hi - lo || 1;
    lo -= span * 0.1;
    hi += span * 0.1;
    let tLo = Math.min(...ts);
    let tHi = Math.max(...ts);
    if (tLo === tHi) {
      tLo -= 1000;
      tHi += 1000;
    }
    return { points: pts, tMin: tLo, tMax: tHi, yMin: lo, yMax: hi };
  }, [series, hidden]);

  const hasAnyMarkers = series.some((s) => s.markers.length > 0);

  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT;
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const tSpan = tMax - tMin || 1;
  const xFor = (t: number) => PAD_LEFT + ((t - tMin) / tSpan) * plotW;
  const yFor = (v: number) => PAD_TOP + plotH - ((v - yMin) / (yMax - yMin)) * plotH;
  const zeroY = yFor(0);

  return (
    <div>
      <div className="mb-2 flex flex-wrap gap-1.5">
        {series.map((s) => (
          <button
            key={s.jobId}
            onClick={() => onToggle(s.jobId)}
            className={`flex items-center gap-1.5 rounded px-2 py-1 text-xs ring-1 transition ${
              hidden.has(s.jobId)
                ? "text-slate-500 ring-slate-800"
                : "text-slate-200 ring-slate-700 hover:bg-slate-800/60"
            }`}
          >
            <span className="h-2 w-2 rounded-full" style={{ background: hidden.has(s.jobId) ? "#475569" : s.color }} />
            <span className="max-w-[10rem] truncate">{s.jobName}</span>
          </button>
        ))}
      </div>

      {/* Kept mounted across every branch below so useContainerWidth has a
          stable element to measure regardless of which state is showing --
          see the same pattern/comment in DeltaChart.tsx. */}
      <div ref={chartRef} className="w-full">
      {!hasAnyMarkers ? (
        <div className="flex h-[220px] items-center justify-center text-sm text-slate-500">No markers yet</div>
      ) : points.length === 0 ? (
        <div className="flex h-[220px] items-center justify-center text-sm text-slate-500">
          No markers with a delta to plot for the visible streams
        </div>
      ) : (
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="h-[220px] w-full" preserveAspectRatio="none">
          <line x1={PAD_LEFT} x2={WIDTH - PAD_RIGHT} y1={zeroY} y2={zeroY} stroke="#475569" strokeWidth={1} />
          <line x1={PAD_LEFT} x2={PAD_LEFT} y1={PAD_TOP} y2={HEIGHT - PAD_BOTTOM} stroke="#334155" strokeWidth={1} />
          <text x={4} y={zeroY + 4} fontSize={10} fill="#94a3b8">
            0ms
          </text>
          <text x={4} y={PAD_TOP + 8} fontSize={10} fill="#94a3b8">
            {yMax.toFixed(0)}
          </text>
          <text x={4} y={HEIGHT - PAD_BOTTOM} fontSize={10} fill="#94a3b8">
            {yMin.toFixed(0)}
          </text>
          {points.map((p) => (
            <circle
              key={`${p.jobId}-${p.marker.id}`}
              cx={xFor(p.t)}
              cy={yFor(p.delta)}
              r={3.5}
              fill={p.color}
              fillOpacity={0.85}
            >
              <title>
                {`${p.jobName} — event_id=${p.marker.event_id ?? "n/a"} delta=${p.delta.toFixed(1)}ms `
                  + `verdict=${p.marker.verdict} wallclock=${p.marker.wallclock ?? ""}`}
              </title>
            </circle>
          ))}
        </svg>
      )}
      </div>
    </div>
  );
}
