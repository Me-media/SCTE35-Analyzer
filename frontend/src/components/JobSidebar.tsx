import { useMemo, useState } from "react";
import type { Job } from "../api/types";

/** Persistent left-hand job switcher, visible alongside every view -- lets
 * you jump straight from one job's detail page to another without going
 * back to the full jobs list first. Each row also carries a checkbox to
 * build up a comparison selection (see CompareView); ticking two or more
 * reveals a "Compare" button here. Selection state itself is owned by
 * App.tsx so it survives navigating around while building it up. */
export default function JobSidebar({
  jobs,
  activeJobId,
  compareSelection,
  onSelect,
  onToggleCompare,
  onClearCompare,
  onStartCompare,
}: {
  jobs: Job[];
  activeJobId: string | null;
  compareSelection: Set<string>;
  onSelect: (id: string) => void;
  onToggleCompare: (id: string) => void;
  onClearCompare: () => void;
  onStartCompare: () => void;
}) {
  const [filter, setFilter] = useState("");

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return jobs;
    return jobs.filter((j) => j.name.toLowerCase().includes(q) || j.id.includes(q));
  }, [jobs, filter]);

  return (
    <aside className="flex w-60 min-h-0 shrink-0 flex-col border-r border-slate-800 bg-slate-950/40">
      <div className="border-b border-slate-800 p-2.5">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter jobs…"
          className="w-full rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-sky-500"
        />
      </div>

      <div className="flex-1 overflow-y-auto">
        {filtered.length === 0 && (
          <div className="p-3 text-xs text-slate-500">{jobs.length === 0 ? "No jobs yet" : "No matches"}</div>
        )}
        {filtered.map((job) => (
          <div
            key={job.id}
            className={`group flex items-center gap-2 border-b border-slate-800/60 px-2.5 py-2 text-xs hover:bg-slate-900/70 ${
              job.id === activeJobId ? "bg-slate-900" : ""
            }`}
          >
            <input
              type="checkbox"
              checked={compareSelection.has(job.id)}
              onChange={() => onToggleCompare(job.id)}
              title="Select for comparison"
              className="h-3.5 w-3.5 shrink-0 rounded border-slate-600 bg-slate-800 accent-sky-500"
            />
            <button
              onClick={() => onSelect(job.id)}
              title={job.name}
              className={`min-w-0 flex-1 truncate text-left ${
                job.id === activeJobId ? "font-medium text-slate-100" : "text-slate-300"
              }`}
            >
              {job.name}
            </button>
            <span
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                job.is_running
                  ? "bg-emerald-500"
                  : job.status === "error"
                    ? "bg-red-500"
                    : "bg-slate-600"
              }`}
              title={job.status}
            />
          </div>
        ))}
      </div>

      {compareSelection.size > 0 && (
        <div className="space-y-2 border-t border-slate-800 p-2.5">
          <div className="flex items-center justify-between text-[11px] text-slate-500">
            <span>{compareSelection.size} selected</span>
            <button onClick={onClearCompare} className="text-slate-500 hover:text-slate-300">
              Clear
            </button>
          </div>
          <button
            onClick={onStartCompare}
            disabled={compareSelection.size < 2}
            className="w-full rounded-md bg-sky-600 py-1.5 text-xs font-medium text-white hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {compareSelection.size < 2
              ? "Select 2+ jobs to compare"
              : `Compare ${compareSelection.size} jobs`}
          </button>
        </div>
      )}
    </aside>
  );
}
