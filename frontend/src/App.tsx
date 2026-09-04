import { useCallback, useEffect, useState } from "react";
import { api } from "./api/client";
import type { Job } from "./api/types";
import CompareView from "./components/CompareView";
import JobDetail from "./components/JobDetail";
import JobList from "./components/JobList";
import JobSidebar from "./components/JobSidebar";
import NewJobForm from "./components/NewJobForm";

type View =
  | { name: "list" }
  | { name: "new" }
  | { name: "detail"; jobId: string }
  | { name: "edit"; jobId: string }
  | { name: "compare"; jobIds: string[] };

export default function App() {
  const [view, setView] = useState<View>({ name: "list" });
  const [jobs, setJobs] = useState<Job[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [version, setVersion] = useState<string | null>(null);
  // Ticked in the left sidebar to build up a comparison; persists across
  // navigation (e.g. tick two jobs while browsing the list, then compare)
  // and survives entering/leaving the compare view itself.
  const [compareSelection, setCompareSelection] = useState<Set<string>>(new Set());

  const refresh = useCallback(() => {
    api.listJobs().then(setJobs).catch(() => {});
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 4000);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    // Shown in the bottom-right corner so a deploy can be confirmed at a
    // glance (see CHANGELOG.md) instead of guessing whether new code
    // landed.
    api.getVersion().then((v) => setVersion(v.version)).catch(() => {});
  }, []);

  async function handleStop(id: string) {
    setActionError(null);
    try {
      await api.stopJob(id);
      refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleRestart(id: string) {
    setActionError(null);
    try {
      await api.restartJob(id);
      refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDelete(id: string) {
    if (!confirm("Delete this job and all its saved markers/snapshots/clips?")) return;
    setActionError(null);
    try {
      await api.deleteJob(id);
      refresh();
      if (view.name === "detail" && view.jobId === id) setView({ name: "list" });
      setCompareSelection((prev) => {
        if (!prev.has(id)) return prev;
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    } catch (e) {
      // Without this, a backend-side failure (e.g. a 500) previously left
      // the job sitting there with zero visible feedback -- the dialog
      // just closed and nothing else happened.
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  function handleToggleCompare(id: string) {
    setCompareSelection((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function handleRemoveFromCompare(id: string) {
    setCompareSelection((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
    // Dropping down to a single job stops being a comparison -- go back
    // to that job's own detail page instead of showing a 1-item compare.
    if (view.name === "compare") {
      const remaining = view.jobIds.filter((j) => j !== id);
      if (remaining.length < 2) {
        setView(remaining.length === 1 ? { name: "detail", jobId: remaining[0] } : { name: "list" });
      } else {
        setView({ name: "compare", jobIds: remaining });
      }
    }
  }

  const activeJobId = view.name === "detail" || view.name === "edit" ? view.jobId : null;

  return (
    <div className="flex h-screen flex-col">
      <header className="border-b border-slate-800 bg-slate-950/80 backdrop-blur shrink-0">
        <div className="flex items-center justify-between px-6 py-4">
          <button onClick={() => setView({ name: "list" })} className="flex items-center gap-2">
            <img src="/logo-mark.svg" alt="" className="h-7 w-7" />
            <span className="text-lg font-semibold text-slate-100">SCTE35 Analyzer</span>
          </button>
          {view.name !== "new" && (
            <button
              onClick={() => setView({ name: "new" })}
              className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500"
            >
              + New job
            </button>
          )}
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <JobSidebar
          jobs={jobs}
          activeJobId={activeJobId}
          compareSelection={compareSelection}
          onSelect={(id) => setView({ name: "detail", jobId: id })}
          onToggleCompare={handleToggleCompare}
          onClearCompare={() => setCompareSelection(new Set())}
          onStartCompare={() => setView({ name: "compare", jobIds: Array.from(compareSelection) })}
        />

        <main className="min-h-0 min-w-0 flex-1 overflow-y-auto px-6 py-8 xl:max-w-6xl">
          {actionError && (
            <div className="mb-4 flex items-start justify-between gap-4 rounded-md bg-red-500/10 px-3 py-2 text-sm text-red-400 ring-1 ring-red-500/30">
              <span>{actionError}</span>
              <button onClick={() => setActionError(null)} className="text-red-400 hover:text-red-300">
                ✕
              </button>
            </div>
          )}
          {view.name === "list" && (
            <JobList
              jobs={jobs}
              onSelect={(id) => setView({ name: "detail", jobId: id })}
              onStop={handleStop}
              onRestart={handleRestart}
              onEdit={(id) => setView({ name: "edit", jobId: id })}
              onDelete={handleDelete}
            />
          )}
          {view.name === "new" && (
            <NewJobForm
              onCreated={(jobId) => {
                refresh();
                setView({ name: "detail", jobId });
              }}
            />
          )}
          {view.name === "detail" && (
            <JobDetail
              jobId={view.jobId}
              onBack={() => setView({ name: "list" })}
              onRestart={handleRestart}
              onEdit={(id) => setView({ name: "edit", jobId: id })}
            />
          )}
          {view.name === "edit" &&
            (() => {
              const job = jobs.find((j) => j.id === view.jobId);
              if (!job) return <div className="p-8 text-sm text-slate-500">Loading…</div>;
              return (
                <NewJobForm
                  editJob={job}
                  onCreated={() => {}}
                  onSaved={() => {
                    refresh();
                    setView({ name: "detail", jobId: job.id });
                  }}
                />
              );
            })()}
          {view.name === "compare" && (
            <CompareView
              jobIds={view.jobIds}
              onBack={() => setView({ name: "list" })}
              onOpenJob={(id) => setView({ name: "detail", jobId: id })}
              onRemove={handleRemoveFromCompare}
            />
          )}
        </main>
      </div>

      {version && (
        <div className="pointer-events-none fixed bottom-3 right-4 text-xs text-slate-600">v{version}</div>
      )}
    </div>
  );
}
