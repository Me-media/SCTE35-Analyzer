import type { Job } from "../api/types";
import Badge, { jobStatusVariant } from "./Badge";

function sourceSummary(job: Job): string {
  const c = job.source_config as Record<string, unknown>;
  switch (job.source_type) {
    case "file":
      return `File: ${c.filename ?? "?"}`;
    case "udp":
      return `UDP ${c.addr}:${c.port} (${c.transport ?? "auto"})`;
    case "hls":
      return `HLS: ${c.url}`;
    case "dash":
      return `DASH: ${c.url}`;
    default:
      return job.source_type;
  }
}

export default function JobList({
  jobs,
  onSelect,
  onStop,
  onRestart,
  onEdit,
  onDelete,
}: {
  jobs: Job[];
  onSelect: (id: string) => void;
  onStop: (id: string) => void;
  onRestart: (id: string) => void;
  onEdit: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  if (jobs.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-slate-800 p-10 text-center text-sm text-slate-500">
        No jobs yet. Start a new job to begin analyzing SCTE-35 markers.
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg ring-1 ring-slate-800">
      <table className="w-full text-sm">
        <thead className="bg-slate-900 text-left text-xs uppercase tracking-wide text-slate-400">
          <tr>
            <th className="px-4 py-2">Name</th>
            <th className="px-4 py-2">Source</th>
            <th className="px-4 py-2">Status</th>
            <th className="px-4 py-2">Created</th>
            <th className="px-4 py-2" />
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">
          {jobs.map((job) => (
            <tr key={job.id} className="hover:bg-slate-900/60">
              <td className="cursor-pointer px-4 py-3" onClick={() => onSelect(job.id)}>
                <div className="font-medium text-slate-100">{job.name}</div>
                <div className="text-xs text-slate-500">{job.id}</div>
              </td>
              <td className="cursor-pointer px-4 py-3 text-slate-300" onClick={() => onSelect(job.id)}>
                {sourceSummary(job)}
                {job.pid_info?.video_codec && (
                  <div className="text-xs text-slate-500">
                    {job.pid_info.video_codec} · video PID {job.pid_info.video_pid} · SCTE-35 PID{" "}
                    {job.pid_info.scte35_pid}
                  </div>
                )}
              </td>
              <td className="px-4 py-3">
                <Badge text={job.status} variant={jobStatusVariant(job.status)} />
                {job.error && <div className="mt-1 max-w-xs text-xs text-red-400">{job.error}</div>}
              </td>
              <td className="px-4 py-3 text-xs text-slate-400">{job.created_at}</td>
              <td className="px-4 py-3 text-right">
                <div className="flex justify-end gap-2">
                  {job.is_running ? (
                    <button
                      onClick={() => onStop(job.id)}
                      className="rounded bg-amber-600/20 px-2 py-1 text-xs text-amber-300 ring-1 ring-amber-600/40 hover:bg-amber-600/30"
                    >
                      Stop
                    </button>
                  ) : (
                    <>
                      <button
                        onClick={() => onRestart(job.id)}
                        className="rounded bg-emerald-600/20 px-2 py-1 text-xs text-emerald-300 ring-1 ring-emerald-600/40 hover:bg-emerald-600/30"
                      >
                        Restart
                      </button>
                      <button
                        onClick={() => onEdit(job.id)}
                        className="rounded bg-slate-700/40 px-2 py-1 text-xs text-slate-300 ring-1 ring-slate-600/40 hover:bg-slate-700/60"
                      >
                        Edit
                      </button>
                    </>
                  )}
                  <button
                    onClick={() => onDelete(job.id)}
                    className="rounded bg-red-600/20 px-2 py-1 text-xs text-red-300 ring-1 ring-red-600/40 hover:bg-red-600/30"
                  >
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
