import type { JobStatus } from "../api/types";

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

export default function Badge({ text, variant }: { text: string; variant: Variant }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap ${VARIANT_CLASSES[variant]}`}
    >
      {text}
    </span>
  );
}
