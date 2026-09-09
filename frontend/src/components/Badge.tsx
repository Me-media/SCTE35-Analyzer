import type { JobStatus, Marker } from "../api/types";

type Variant = "ok" | "warn" | "bad" | "missed" | "neutral";

const VARIANT_CLASSES: Record<Variant, string> = {
  ok: "bg-emerald-500/15 text-emerald-400 ring-1 ring-emerald-500/30",
  warn: "bg-amber-500/15 text-amber-400 ring-1 ring-amber-500/30",
  bad: "bg-red-500/15 text-red-400 ring-1 ring-red-500/30",
  missed: "bg-slate-500/15 text-slate-400 ring-1 ring-slate-500/30",
  neutral: "bg-sky-500/15 text-sky-400 ring-1 ring-sky-500/30",
};

export function verdictVariant(verdict: string | null | undefined): Variant {
  if (!verdict) return "neutral";
  if (verdict.startsWith("OK")) return "ok";
  if (verdict.startsWith("OUT_OF_SPEC")) return "bad";
  if (verdict.startsWith("REVIEW")) return "warn";
  if (verdict.startsWith("MISSED")) return "missed";
  if (verdict.startsWith("SIGNAL_LATE") || verdict.startsWith("PREROLL_SHORT")) return "warn";
  if (verdict.startsWith("GOP_WAIT")) return "warn";
  if (verdict.startsWith("FORCED")) return "ok";
  if (verdict.startsWith("UNCLEAR")) return "neutral";
  if (verdict.startsWith("RETRANSMISSION") || verdict === "N/A") return "neutral";
  return "neutral";
}

export type VerdictField = "verdict" | "preroll_verdict" | "signal_verdict" | "gop_verdict";

/** Tooltip text for a verdict badge -- what this specific value actually
 * means, so a viewer doesn't have to remember it or go dig through the
 * README/module docstring. Matched by prefix (like verdictVariant above)
 * since `verdict` and `preroll_verdict`'s MISSED/PREROLL_SHORT etc. values
 * are enum-like but not always byte-for-byte identical strings. Field-aware
 * on purpose: "OK" and "N/A" mean something different in each of the four
 * *_verdict fields, so this can't be a single flat lookup by value alone. */
export function verdictExplanation(field: VerdictField, verdict: string | null | undefined): string | undefined {
  if (!verdict) return undefined;
  if (field === "verdict") {
    if (verdict.startsWith("OK"))
      return "delta_ms (matched IDR's PTS minus the cue's own target PTS) is within the configured OK threshold -- a clean splice point.";
    if (verdict.startsWith("OUT_OF_SPEC"))
      return "delta_ms exceeds the configured OK threshold -- the matched IDR landed too far from the cue's target PTS for a clean splice.";
    if (verdict.startsWith("REVIEW"))
      return "The nearest splice point landed on a CRA frame, not a true IDR. CRA pictures aren't fully independent (leading pictures may reference before it), so treat this as needing manual review, not automatically OK.";
    if (verdict.startsWith("MISSED"))
      return "No IDR matched this cue's target PTS before the timeout elapsed (or the run/file ended) -- a failed splice opportunity.";
    return undefined;
  }
  if (field === "preroll_verdict") {
    if (verdict === "OK")
      return "The real wall-clock time between registering this cue and observing the matching IDR was at or above what the cue's own PTS-domain timing (time_to_event_ms) declared, within tolerance.";
    if (verdict === "PREROLL_SHORT")
      return "The actual real-time lead time this machine measured fell materially short of what the cue's own timing declared -- downstream ad-decisioning/splicing may have gotten less real warning than promised. Only meaningful for a genuinely live feed, not a file replayed faster/slower than real time.";
    if (verdict === "N/A")
      return "Not applicable -- this cue was MISSED, so there's no actual pre-roll to measure.";
    return undefined;
  }
  if (field === "signal_verdict") {
    if (verdict === "OK")
      return "This event's first transmission was signaled at least the configured minimum advance notice (default 4000ms) before its own splice time.";
    if (verdict === "SIGNAL_LATE")
      return "This event's first transmission was signaled less than the configured minimum advance notice before its own splice time -- the encoder/packager may not have had enough time to react cleanly.";
    if (verdict === "RETRANSMISSION")
      return "A repeat transmission of an already-seen splice_event_id (normal practice, for resilience against packet loss) -- not re-evaluated against the minimum advance-notice requirement.";
    if (verdict === "N/A")
      return "No time_to_event_ms could be computed -- no video PTS had been observed yet when this cue arrived.";
    return undefined;
  }
  if (field === "gop_verdict") {
    if (verdict === "FORCED")
      return "delta_ms is small (already verdict=OK) -- consistent with the encoder forcing a real keyframe at the splice point. Any remaining ad-start lateness is downstream of this probe (packager/ad-decisioning).";
    if (verdict === "GOP_WAIT")
      return "delta_ms lands close to a whole multiple of the stream's sampled GOP duration -- the encoder most likely never forced a keyframe at the splice point, and the packager just cut at its next naturally-scheduled IDR instead. Likely root cause of a systematically-late ad start.";
    if (verdict === "UNCLEAR")
      return "delta_ms fits neither the forced-keyframe nor the GOP-cadence pattern (e.g. a variable-GOP encoder) -- inconclusive.";
    if (verdict === "N/A")
      return "No GOP sample has landed yet (needs a video_info sample, ~20s interval), or this cue was MISSED.";
    return undefined;
  }
  return undefined;
}

function fmtMsValue(v: number | null | undefined): string {
  if (v === null || v === undefined) return "n/a";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(1)}ms`;
}

/** The concrete number(s) behind a specific verdict badge for a specific
 * marker row -- e.g. "delta_ms: +20.0ms" -- appended as a second line
 * under verdictExplanation()'s generic category text (see verdictTooltip
 * below), so hovering a badge answers both "what does this verdict mean"
 * and "why did THIS row get it" without having to cross-reference other
 * columns. Field-aware, same as verdictExplanation, since the value that
 * actually explains a verdict differs per field:
 *   verdict          -> delta_ms, or -- for MISSED -- the near_miss_ms
 *                        diagnostic (was a rejected IDR candidate seen
 *                        nearby, and how far off was it?)
 *   gop_verdict       -> delta_ms (compared against the sampled GOP
 *                        duration to produce FORCED/GOP_WAIT/UNCLEAR)
 *   preroll_verdict   -> the declared/actual/delta trio it's computed from
 *   signal_verdict    -> time_to_event_ms (the declared lead time it's
 *                        judged against the minimum advance-notice figure)
 */
export function verdictValueLine(field: VerdictField, marker: Marker): string | undefined {
  if (field === "verdict") {
    if (marker.verdict && marker.verdict.startsWith("MISSED")) {
      if (marker.near_miss_ms !== null && marker.near_miss_ms !== undefined) {
        const direction = marker.near_miss_ms > 0 ? "late" : "early";
        return `Nearest rejected IDR candidate: ${fmtMsValue(marker.near_miss_ms)} (too ${direction} to match)`;
      }
      return "No IDR was seen anywhere near the target PTS while this cue was pending.";
    }
    return `delta_ms: ${fmtMsValue(marker.delta_ms)}`;
  }
  if (field === "gop_verdict") {
    return `delta_ms: ${fmtMsValue(marker.delta_ms)}`;
  }
  if (field === "preroll_verdict") {
    return (
      `declared time_to_event_ms: ${fmtMsValue(marker.time_to_event_ms)}  ·  ` +
      `actual_preroll_ms: ${fmtMsValue(marker.actual_preroll_ms)}  ·  ` +
      `preroll_delta_ms: ${fmtMsValue(marker.preroll_delta_ms)}`
    );
  }
  if (field === "signal_verdict") {
    return `time_to_event_ms: ${fmtMsValue(marker.time_to_event_ms)}`;
  }
  return undefined;
}

/** Full tooltip text for a verdict badge: verdictExplanation()'s generic
 * "what this category means" followed by verdictValueLine()'s concrete
 * "why THIS row got it", on its own line (native `title` tooltips render
 * \n as a line break). Use this instead of calling verdictExplanation
 * directly wherever a Marker is on hand. */
export function verdictTooltip(field: VerdictField, marker: Marker): string | undefined {
  const explanation = verdictExplanation(field, marker[field]);
  if (!explanation) return undefined;
  const valueLine = verdictValueLine(field, marker);
  return valueLine ? `${explanation}\n\n${valueLine}` : explanation;
}

export function jobStatusVariant(status: JobStatus): Variant {
  switch (status) {
    case "running":
      return "ok";
    case "finished":
    case "stopped":
      return "neutral";
    case "error":
      return "bad";
    case "starting":
    default:
      return "warn";
  }
}

export default function Badge({ text, variant, title }: { text: string; variant: Variant; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap ${VARIANT_CLASSES[variant]}${title ? " cursor-help" : ""}`}
    >
      {text}
    </span>
  );
}
