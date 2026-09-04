import { useMemo } from "react";
import type { Marker } from "../api/types";
import { verdictVariant } from "./Badge";

const DOT_COLOR: Record<string, string> = {
  ok: "#22c55e",
  warn: "#f59e0b",
  bad: "#ef4444",
  missed: "#94a3b8",
  neutral: "#38bdf8",
};

const WIDTH = 900;
const HEIGHT = 220;
const PAD_LEFT = 56;
const PAD_RIGHT = 16;
const PAD_TOP = 16;
const PAD_BOTTOM = 28;
const MISSED_ROW_Y = PAD_TOP + 10;

/** Plain SVG scatter of delta_ms (SCTE-35 target PTS vs. matched IDR PTS)
 * per marker, in arrival order -- no charting library, so this stays
 * dependency-free. A MISSED marker (no delta_ms) is drawn on its own row
 * at the top instead of being silently dropped from the plot. */
export default function DeltaChart({ markers, okThresholdMs }: { markers: Marker[]; okThresholdMs: number }) {
  const { points, missed, yMin, yMax } = useMemo(() => {
    const indexed = markers.map((m, idx) => ({ m, idx }));
    const withDelta = indexed.filter(({ m }) => m.delta_ms !== null) as {
      m: Marker & { delta_ms: number };
      idx: number;
    }[];
    const missedList = indexed.filter(({ m }) => m.delta_ms === null);
    const values = withDelta.map(({ m }) => m.delta_ms);
    let lo = Math.min(0, ...values, -okThresholdMs);
    let hi = Math.max(0, ...values, okThresholdMs);
    if (!isFinite(lo)) lo = -okThresholdMs;
    if (!isFinite(hi)) hi = okThresholdMs;
    const span = hi - lo || 1;
    lo -= span * 0.1;
    hi += span * 0.1;
    return { points: withDelta, missed: missedList, yMin: lo, yMax: hi };
  }, [markers, okThresholdMs]);

  if (markers.length === 0) {
    return (
      <div className="flex h-[220px] items-center justify-center text-sm text-slate-500">
        No markers yet
      </div>
    );
  }

  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT;
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM - 22; // reserve a lane for missed dots
  const n = Math.max(markers.length - 1, 1);
  const xFor = (i: number) => PAD_LEFT + (i / n) * plotW;
  const yFor = (v: number) => PAD_TOP + 22 + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  const zeroY = yFor(0);
  const okTopY = yFor(okThresholdMs);
  const okBotY = yFor(-okThresholdMs);

  return (
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
      <line
        x1={PAD_LEFT}
        x2={PAD_LEFT}
        y1={PAD_TOP}
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

      {missed.map(({ m, idx }) => (
        <circle key={`missed-${m.id}`} cx={xFor(idx)} cy={MISSED_ROW_Y} r={3.5} fill={DOT_COLOR.missed}>
          <title>
            {`event_id=${m.event_id ?? "n/a"} verdict=${m.verdict} wallclock=${m.wallclock ?? ""}`}
          </title>
        </circle>
      ))}

      {points.map(({ m, idx }) => (
        <circle
          key={m.id}
          cx={xFor(idx)}
          cy={yFor(m.delta_ms)}
          r={3.5}
          fill={DOT_COLOR[verdictVariant(m.verdict)]}
        >
          <title>
            {`event_id=${m.event_id ?? "n/a"} delta=${m.delta_ms.toFixed(1)}ms verdict=${m.verdict} wallclock=${m.wallclock ?? ""}`}
          </title>
        </circle>
      ))}
    </svg>
  );
}
