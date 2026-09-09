import { Fragment, useMemo, useState } from "react";
import type { CueSnapshot, Marker, Segment } from "../api/types";
import { basename, api } from "../api/client";
import { formatLocal, localZoneName } from "../lib/time";
import Badge, { verdictExplanation, verdictVariant } from "./Badge";

const CUE_SNAPSHOT_LABELS: Record<CueSnapshot["tag"], string> = {
  time_to_event_cue_arrival: "Time-to-event: frame on air when the cue arrived",
  time_to_event_target_pts: "Time-to-event: frame at the literal target PTS",
  preroll_realtime_deadline: "Pre-roll: frame at the real-time deadline",
  preroll_same_as_matched_idr: "Pre-roll: same frame as the matched IDR snapshot",
};

function fmtMs(v: number | null): string {
  if (v === null || v === undefined) return "–";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(1)}ms`;
}

function fmtS(v: number | null): string {
  if (v === null || v === undefined) return "–";
  return `${v.toFixed(3)}s`;
}

export default function MarkerTable({
  jobId,
  markers,
  segments,
  cueSnapshots,
  onOpenSegment,
  onOpenImage,
}: {
  jobId: string;
  markers: Marker[];
  segments: Segment[];
  cueSnapshots: CueSnapshot[];
  onOpenSegment: (segment: Segment) => void;
  onOpenImage: (src: string, caption?: string) => void;
}) {
  const [expanded, setExpanded] = useState<number | null>(null);

  const segmentsByCueSeq = useMemo(() => {
    const map = new Map<number, Segment[]>();
    for (const seg of segments) {
      for (const seq of seg.event_cue_seqs) {
        const arr = map.get(seq) ?? [];
        arr.push(seg);
        map.set(seq, arr);
      }
    }
    return map;
  }, [segments]);

  const cueSnapshotsByCueSeq = useMemo(() => {
    const map = new Map<number, CueSnapshot[]>();
    for (const cs of cueSnapshots) {
      if (cs.cue_seq === null) continue;
      const arr = map.get(cs.cue_seq) ?? [];
      arr.push(cs);
      map.set(cs.cue_seq, arr);
    }
    return map;
  }, [cueSnapshots]);

  const ordered = useMemo(() => [...markers].reverse(), [markers]);

  if (markers.length === 0) {
    return <div className="p-6 text-sm text-slate-500">No SCTE-35 markers found yet.</div>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-slate-900/95 backdrop-blur text-left text-xs uppercase tracking-wide text-slate-400">
          <tr>
            <th className="px-3 py-2" title="Every timestamp in this tool is stored and reported in UTC">
              Time (UTC)
            </th>
            <th className="px-3 py-2" title={`Viewer's local time (${localZoneName()}) -- not stored, not in exports`}>
              Local time
            </th>
            <th className="px-3 py-2">Event ID</th>
            <th className="px-3 py-2">Type</th>
            <th className="px-3 py-2">Target PTS</th>
            <th className="px-3 py-2">IDR PTS</th>
            <th className="px-3 py-2">Delta</th>
            <th className="px-3 py-2">Verdict</th>
            <th
              className="px-3 py-2"
              title="Heuristic: did the encoder likely force a real keyframe at the splice point (FORCED), or did it just fall through to its next natural GOP boundary instead (GOP_WAIT, the usual cause of ads starting later than the cue's own PTS)? Requires --video-info-enabled; N/A until a GOP sample has landed."
            >
              GOP
            </th>
            <th className="px-3 py-2">Pre-roll</th>
            <th className="px-3 py-2">Signal</th>
            <th className="px-3 py-2">Snapshot</th>
            <th className="px-3 py-2">Clip</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/70">
          {ordered.map((m) => {
            const segs = m.cue_seq !== null ? (segmentsByCueSeq.get(m.cue_seq) ?? []) : [];
            const isOpen = expanded === m.id;
            return (
              <Fragment key={m.id}>
                <tr
                  className="cursor-pointer hover:bg-slate-800/40"
                  onClick={() => setExpanded(isOpen ? null : m.id)}
                >
                  <td className="px-3 py-2 mono text-xs text-slate-400 whitespace-nowrap">
                    {m.wallclock ?? "–"}
                  </td>
                  <td className="px-3 py-2 mono text-xs text-slate-500 whitespace-nowrap">
                    {m.wallclock ? formatLocal(m.wallclock) : "–"}
                  </td>
                  <td className="px-3 py-2 mono">{m.event_id ?? "–"}</td>
                  <td className="px-3 py-2 text-xs text-slate-400">{m.command_type ?? "–"}</td>
                  <td className="px-3 py-2 mono text-xs">{fmtS(m.target_pts_s)}</td>
                  <td className="px-3 py-2 mono text-xs">{fmtS(m.idr_pts_s)}</td>
                  <td className="px-3 py-2 mono text-xs">{fmtMs(m.delta_ms)}</td>
                  <td className="px-3 py-2">
                    <Badge
                      text={m.verdict ?? "–"}
                      variant={verdictVariant(m.verdict)}
                      title={verdictExplanation("verdict", m.verdict)}
                    />
                  </td>
                  <td className="px-3 py-2">
                    {m.gop_verdict && m.gop_verdict !== "N/A" && (
                      <Badge
                        text={m.gop_verdict}
                        variant={verdictVariant(m.gop_verdict)}
                        title={verdictExplanation("gop_verdict", m.gop_verdict)}
                      />
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {m.preroll_verdict && m.preroll_verdict !== "N/A" && (
                      <Badge
                        text={m.preroll_verdict}
                        variant={verdictVariant(m.preroll_verdict)}
                        title={verdictExplanation("preroll_verdict", m.preroll_verdict)}
                      />
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {m.signal_verdict && m.signal_verdict !== "N/A" && (
                      <Badge
                        text={m.signal_verdict}
                        variant={verdictVariant(m.signal_verdict)}
                        title={verdictExplanation("signal_verdict", m.signal_verdict)}
                      />
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {m.snapshot_path && (
                      <img
                        src={api.snapshotUrl(jobId, basename(m.snapshot_path))}
                        alt="IDR snapshot"
                        className="h-10 w-16 cursor-zoom-in rounded object-cover ring-1 ring-slate-700"
                        loading="lazy"
                        onClick={(e) => {
                          e.stopPropagation();
                          onOpenImage(
                            api.snapshotUrl(jobId, basename(m.snapshot_path)),
                            `Matched IDR snapshot — event_id=${m.event_id ?? "n/a"}`,
                          );
                        }}
                      />
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {segs.map((seg) => (
                      <button
                        key={seg.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          onOpenSegment(seg);
                        }}
                        className="rounded bg-sky-600/20 px-2 py-1 text-xs text-sky-300 ring-1 ring-sky-600/40 hover:bg-sky-600/30"
                      >
                        ▶ Play
                      </button>
                    ))}
                  </td>
                </tr>
                {isOpen && (
                  <tr className="bg-slate-900/60">
                    <td colSpan={13} className="px-3 py-3">
                      <MarkerDetail
                        jobId={jobId}
                        marker={m}
                        cueSnapshots={m.cue_seq !== null ? (cueSnapshotsByCueSeq.get(m.cue_seq) ?? []) : []}
                        onOpenImage={onOpenImage}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function MarkerDetail({
  jobId,
  marker,
  cueSnapshots,
  onOpenImage,
}: {
  jobId: string;
  marker: Marker;
  cueSnapshots: CueSnapshot[];
  onOpenImage: (src: string, caption?: string) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-x-8 gap-y-1 text-xs md:grid-cols-4">
      <Field label="cue_seq" value={marker.cue_seq} />
      <Field label="out_of_network" value={String(marker.out_of_network)} />
      <Field label="codec / au_kind" value={`${marker.codec ?? "–"} / ${marker.au_kind ?? "–"}`} />
      <Field label="raw_pts_time_s" value={marker.raw_pts_time_s} />
      <Field label="pts_adjustment_ticks" value={marker.pts_adjustment_ticks} />
      <Field label="time_to_event_ms" value={fmtMs(marker.time_to_event_ms)} />
      <Field label="actual_preroll_ms" value={fmtMs(marker.actual_preroll_ms)} />
      <Field label="preroll_delta_ms" value={fmtMs(marker.preroll_delta_ms)} />
      <div className="col-span-2 md:col-span-4">
        <span className="text-slate-500">segmentation: </span>
        <span className="mono text-slate-300">
          {marker.segmentation_summary.length ? marker.segmentation_summary.join("; ") : "–"}
        </span>
      </div>
      {marker.pre_frame_snapshot_paths.length > 0 && (
        <div className="col-span-2 md:col-span-4">
          <span className="mb-1 block text-slate-500">pre-frames:</span>
          <div className="flex gap-2">
            {marker.pre_frame_snapshot_paths.map((p) => (
              <img
                key={p}
                src={api.snapshotUrl(jobId, basename(p))}
                className="h-12 w-20 cursor-zoom-in rounded object-cover ring-1 ring-slate-700"
                onClick={() => onOpenImage(api.snapshotUrl(jobId, basename(p)), "Pre-frame snapshot")}
              />
            ))}
          </div>
        </div>
      )}
      {cueSnapshots.length > 0 && (
        <div className="col-span-2 md:col-span-4">
          <span className="mb-1 block text-slate-500">reference snapshots:</span>
          <div className="flex flex-wrap gap-3">
            {cueSnapshots.map((cs) => (
              <div key={cs.id} className="flex flex-col items-start gap-1">
                <img
                  src={api.snapshotUrl(jobId, basename(cs.path))}
                  className="h-16 w-28 cursor-zoom-in rounded object-cover ring-1 ring-slate-700"
                  onClick={() =>
                    onOpenImage(api.snapshotUrl(jobId, basename(cs.path)), CUE_SNAPSHOT_LABELS[cs.tag] ?? cs.tag)
                  }
                />
                <span className="max-w-[7rem] text-[10px] leading-tight text-slate-400">
                  {CUE_SNAPSHOT_LABELS[cs.tag] ?? cs.tag}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <span className="text-slate-500">{label}: </span>
      <span className="mono text-slate-300">{value === null || value === undefined ? "–" : String(value)}</span>
    </div>
  );
}
