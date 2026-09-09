import { useEffect, useRef, useState } from "react";
import { api, connectJobSocket } from "../api/client";
import type { Job, Marker, WsMessage } from "../api/types";
import Badge, { jobStatusVariant, verdictVariant } from "./Badge";
import CompareChart from "./CompareChart";

const COLORS = ["#38bdf8", "#f472b6", "#facc15", "#4ade80", "#a78bfa", "#fb923c", "#2dd4bf", "#f87171"];

interface JobStream {
  job: Job;
  markers: Marker[];
  connected: boolean;
}

function summarize(markers: Marker[]) {
  let ok = 0;
  let warn = 0;
  let bad = 0;
  let missed = 0;
  const deltas: number[] = [];
  for (const m of markers) {
    const v = verdictVariant(m.verdict);
    if (v === "ok") ok++;
    else if (v === "warn") warn++;
    else if (v === "bad") bad++;
    else if (v === "missed") missed++;
    if (m.delta_ms !== null) deltas.push(m.delta_ms);
  }
  const avgAbsDelta = deltas.length ? deltas.reduce((a, b) => a + Math.abs(b), 0) / deltas.length : null;
  return { total: markers.length, ok, warn, bad, missed, avgAbsDelta };
}

/** Side-by-side live comparison of two or more jobs -- e.g. a primary vs.
 * a redundant/backup encoder watching the same event, or the same file
 * reprocessed under different tuning. Each selected job gets its own
 * WebSocket subscription (same mechanism as JobDetail) so the comparison
 * stays live, not a one-off snapshot. */
export default function CompareView({
  jobIds,
  onBack,
  onOpenJob,
  onRemove,
}: {
  jobIds: string[];
  onBack: () => void;
  onOpenJob: (id: string) => void;
  onRemove: (id: string) => void;
}) {
  const [streams, setStreams] = useState<Record<string, JobStream>>({});
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const seenMarkerIds = useRef<Record<string, Set<number>>>({});

  useEffect(() => {
    let cancelled = false;
    const disconnects: (() => void)[] = [];
    seenMarkerIds.current = {};
    setStreams({});

    for (const jobId of jobIds) {
      seenMarkerIds.current[jobId] = new Set();

      Promise.all([api.getJob(jobId), api.listMarkers(jobId)])
        .then(([job, markers]) => {
          if (cancelled) return;
          for (const m of markers) seenMarkerIds.current[jobId].add(m.id);
          setStreams((prev) => ({ ...prev, [jobId]: { job, markers, connected: false } }));
        })
        .catch(() => {});

      const disconnect = connectJobSocket(
        jobId,
        (msg: WsMessage) => {
          if (cancelled) return;
          if (msg.kind === "match_result") {
            const m = msg.data as Marker;
            if (seenMarkerIds.current[jobId]?.has(m.id)) return;
            seenMarkerIds.current[jobId]?.add(m.id);
            setStreams((prev) => {
              const s = prev[jobId];
              if (!s) return prev;
              return { ...prev, [jobId]: { ...s, markers: [...s.markers, m] } };
            });
          } else if (msg.kind === "status") {
            setStreams((prev) => {
              const s = prev[jobId];
              if (!s) return prev;
              return {
                ...prev,
                [jobId]: {
                  ...s,
                  job: {
                    ...s.job,
                    status: msg.data.status,
                    error: msg.data.message ?? s.job.error,
                    is_running: msg.data.status === "running",
                  },
                },
              };
            });
          }
        },
        (connected) => {
          if (cancelled) return;
          setStreams((prev) => {
            const s = prev[jobId];
            if (!s) return prev;
            return { ...prev, [jobId]: { ...s, connected } };
          });
        },
      );
      disconnects.push(disconnect);
    }

    return () => {
      cancelled = true;
      disconnects.forEach((d) => d());
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobIds.join(",")]);

  const entries = jobIds.map((id, i) => ({ id, color: COLORS[i % COLORS.length] }));

  const mergedRows = entries
    .flatMap(({ id, color }) => {
      const s = streams[id];
      if (!s) return [];
      return s.markers.map((m) => ({ jobId: id, jobName: s.job.name, color, marker: m }));
    })
    .filter((r) => !hidden.has(r.jobId))
    .sort((a, b) => (a.marker.wallclock ?? "").localeCompare(b.marker.wallclock ?? ""));

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <button onClick={onBack} className="text-sm text-slate-400 hover:text-slate-200">
          ← All jobs
        </button>
        <span className="text-xs text-slate-500">Comparing {jobIds.length} jobs, live</span>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {entries.map(({ id, color }) => {
          const s = streams[id];
          if (!s) {
            return (
              <div key={id} className="rounded-lg bg-slate-900 p-3 text-xs text-slate-500 ring-1 ring-slate-800">
                Loading…
              </div>
            );
          }
          const stats = summarize(s.markers);
          return (
            <div
              key={id}
              className="space-y-2 rounded-lg bg-slate-900 p-3 ring-1 ring-slate-800"
              style={{ borderTop: `3px solid ${color}` }}
            >
              <div className="flex items-start justify-between gap-2">
                <button
                  onClick={() => onOpenJob(id)}
                  className="min-w-0 truncate text-left text-sm font-medium text-slate-100 hover:text-sky-400"
                  title={s.job.name}
                >
                  {s.job.name}
                </button>
                <button
                  onClick={() => onRemove(id)}
                  className="shrink-0 text-slate-500 hover:text-red-400"
                  title="Remove from comparison"
                >
                  ✕
                </button>
              </div>
              <div className="flex items-center gap-2">
                <Badge text={s.job.status} variant={jobStatusVariant(s.job.status)} />
                <span className={`h-1.5 w-1.5 rounded-full ${s.connected ? "bg-emerald-500" : "bg-slate-600"}`} />
              </div>
              {s.job.pid_info?.video_codec && (
                <p className="text-[11px] text-slate-500">
                  {s.job.pid_info.video_codec} · video PID {s.job.pid_info.video_pid} · SCTE-35 PID{" "}
                  {s.job.pid_info.scte35_pid}
                </p>
              )}
              <div className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-[11px] text-slate-400">
                <span>Markers: {stats.total}</span>
                <span>Missed: {stats.missed}</span>
                <span>OK: {stats.ok}</span>
                <span>Warn/bad: {stats.warn + stats.bad}</span>
                <span className="col-span-2">
                  Avg |delta|: {stats.avgAbsDelta === null ? "–" : `${stats.avgAbsDelta.toFixed(1)}ms`}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      <section className="rounded-lg bg-slate-900 p-4 ring-1 ring-slate-800">
        <h3 className="mb-2 text-sm font-medium text-slate-300">
          Delta comparison (real wall-clock time, colored by stream)
        </h3>
        <CompareChart
          series={entries.map(({ id, color }) => ({
            jobId: id,
            jobName: streams[id]?.job.name ?? id,
            color,
            markers: streams[id]?.markers ?? [],
          }))}
          hidden={hidden}
          onToggle={(jobId) =>
            setHidden((prev) => {
              const next = new Set(prev);
              if (next.has(jobId)) next.delete(jobId);
              else next.add(jobId);
              return next;
            })
          }
        />
      </section>

      <section className="rounded-lg bg-slate-900 ring-1 ring-slate-800">
        <h3 className="border-b border-slate-800 px-4 py-3 text-sm font-medium text-slate-300">
          Markers, merged and time-ordered ({mergedRows.length})
        </h3>
        {mergedRows.length === 0 ? (
          <div className="p-6 text-sm text-slate-500">No markers yet for the visible streams.</div>
        ) : (
          <div className="max-h-[480px] overflow-y-auto overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-slate-900/95 backdrop-blur text-left text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-3 py-2" title="Every timestamp in this tool is stored and reported in UTC">
                    Time (UTC)
                  </th>
                  <th className="px-3 py-2">Stream</th>
                  <th className="px-3 py-2">Event ID</th>
                  <th className="px-3 py-2">Delta</th>
                  <th className="px-3 py-2">Verdict</th>
                  <th
                    className="px-3 py-2"
                    title="Heuristic: did the encoder likely force a keyframe at the splice point (FORCED), or fall through to its next natural GOP boundary (GOP_WAIT)? Requires --video-info-enabled."
                  >
                    GOP
                  </th>
                  <th className="px-3 py-2">Pre-roll</th>
                  <th className="px-3 py-2">Signal</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/70">
                {mergedRows.map(({ jobId, jobName, color, marker: m }) => (
                  <tr key={`${jobId}-${m.id}`} className="hover:bg-slate-800/40">
                    <td className="px-3 py-2 mono text-xs text-slate-400 whitespace-nowrap">{m.wallclock ?? "–"}</td>
                    <td className="px-3 py-2">
                      <span className="inline-flex items-center gap-1.5 text-xs text-slate-300">
                        <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: color }} />
                        <span className="max-w-[9rem] truncate" title={jobName}>
                          {jobName}
                        </span>
                      </span>
                    </td>
                    <td className="px-3 py-2 mono">{m.event_id ?? "–"}</td>
                    <td className="px-3 py-2 mono text-xs">
                      {m.delta_ms === null ? "–" : `${m.delta_ms > 0 ? "+" : ""}${m.delta_ms.toFixed(1)}ms`}
                    </td>
                    <td className="px-3 py-2">
                      <Badge text={m.verdict ?? "–"} variant={verdictVariant(m.verdict)} />
                    </td>
                    <td className="px-3 py-2">
                      {m.gop_verdict && m.gop_verdict !== "N/A" && (
                        <Badge text={m.gop_verdict} variant={verdictVariant(m.gop_verdict)} />
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {m.preroll_verdict && m.preroll_verdict !== "N/A" && (
                        <Badge text={m.preroll_verdict} variant={verdictVariant(m.preroll_verdict)} />
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {m.signal_verdict && m.signal_verdict !== "N/A" && (
                        <Badge text={m.signal_verdict} variant={verdictVariant(m.signal_verdict)} />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
