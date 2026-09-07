import { useEffect, useRef, useState } from "react";
import { api, connectJobSocket } from "../api/client";
import { CLEANUP_AGE_PRESETS, RETENTION_PRESETS } from "../api/types";
import type { CueSnapshot, Job, Marker, Segment, WsMessage } from "../api/types";
import Badge, { jobStatusVariant } from "./Badge";
import DeltaChart from "./DeltaChart";
import ImageLightbox from "./ImageLightbox";
import MarkerTable from "./MarkerTable";
import SegmentPlayerModal from "./SegmentPlayerModal";

export default function JobDetail({
  jobId,
  onBack,
  onRestart,
  onEdit,
}: {
  jobId: string;
  onBack: () => void;
  onRestart: (id: string) => void;
  onEdit: (id: string) => void;
}) {
  const [job, setJob] = useState<Job | null>(null);
  const [markers, setMarkers] = useState<Marker[]>([]);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [cueSnapshots, setCueSnapshots] = useState<CueSnapshot[]>([]);
  const [wsConnected, setWsConnected] = useState(false);
  const [playingSegment, setPlayingSegment] = useState<Segment | null>(null);
  const [viewingImage, setViewingImage] = useState<{ src: string; caption?: string } | null>(null);
  const [refreshingVideoInfo, setRefreshingVideoInfo] = useState(false);
  const [savingRetention, setSavingRetention] = useState<"segment" | "snapshot" | null>(null);
  const [cleanupAge, setCleanupAge] = useState<{ segment: number; snapshot: number }>({
    segment: CLEANUP_AGE_PRESETS[0].valueS,
    snapshot: CLEANUP_AGE_PRESETS[0].valueS,
  });
  const [cleaningUp, setCleaningUp] = useState<"segment" | "snapshot" | null>(null);
  const [cleanupResult, setCleanupResult] = useState<{ category: "segment" | "snapshot"; message: string } | null>(
    null,
  );
  const [cleanupMenuOpen, setCleanupMenuOpen] = useState(false);
  const [openCleanupCategory, setOpenCleanupCategory] = useState<"segment" | "snapshot" | null>(null);
  const [flushing, setFlushing] = useState(false);
  const [flushResult, setFlushResult] = useState<string | null>(null);
  const markerIds = useRef<Set<number>>(new Set());
  const segmentIds = useRef<Set<number>>(new Set());
  const cueSnapshotIds = useRef<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    markerIds.current = new Set();
    segmentIds.current = new Set();
    cueSnapshotIds.current = new Set();
    setMarkers([]);
    setSegments([]);
    setCueSnapshots([]);
    setJob(null);

    api.getJob(jobId).then((j) => !cancelled && setJob(j));

    const disconnect = connectJobSocket(
      jobId,
      (msg: WsMessage) => {
        if (cancelled) return;
        handleMessage(msg);
      },
      setWsConnected,
    );
    return () => {
      cancelled = true;
      disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  function handleMessage(msg: WsMessage) {
    switch (msg.kind) {
      case "pid_info":
        setJob((j) => (j ? { ...j, pid_info: { ...j.pid_info, ...msg.data } } : j));
        break;
      case "video_info":
        setJob((j) => (j ? { ...j, video_info: msg.data } : j));
        break;
      case "match_result": {
        const m = msg.data as Marker;
        if (markerIds.current.has(m.id)) break;
        markerIds.current.add(m.id);
        setMarkers((prev) => [...prev, m]);
        break;
      }
      case "segment_saved": {
        const s = msg.data as Segment;
        if (s.id === undefined || segmentIds.current.has(s.id)) break;
        segmentIds.current.add(s.id);
        setSegments((prev) => [...prev, s]);
        break;
      }
      case "reference_snapshot_saved": {
        const cs = msg.data as CueSnapshot;
        if (cs.id === undefined || cueSnapshotIds.current.has(cs.id)) break;
        cueSnapshotIds.current.add(cs.id);
        setCueSnapshots((prev) => [...prev, cs]);
        break;
      }
      case "status":
        setJob((j) =>
          j ? { ...j, status: msg.data.status, error: msg.data.message ?? j.error, is_running: msg.data.status === "running" } : j,
        );
        break;
      default:
        break;
    }
  }

  async function handleRetentionChange(category: "segment" | "snapshot", valueS: number | null) {
    setSavingRetention(category);
    try {
      const body =
        category === "segment"
          ? { segment_retention_s: valueS, snapshot_retention_s: job?.snapshot_retention_s ?? null }
          : { segment_retention_s: job?.segment_retention_s ?? null, snapshot_retention_s: valueS };
      const updated = await api.updateRetention(jobId, body);
      setJob((j) =>
        j
          ? { ...j, segment_retention_s: updated.segment_retention_s, snapshot_retention_s: updated.snapshot_retention_s }
          : j,
      );
    } finally {
      setSavingRetention(null);
    }
  }

  async function handleCleanupNow(category: "segment" | "snapshot") {
    setCleaningUp(category);
    setCleanupResult(null);
    try {
      const body =
        category === "segment"
          ? { segment_max_age_s: cleanupAge.segment }
          : { snapshot_max_age_s: cleanupAge.snapshot };
      const result = await api.cleanupJob(jobId, body);
      const count = category === "segment" ? result.segments_deleted : result.snapshot_files_deleted;
      const noun = category === "segment" ? "segment" : "snapshot file";
      setCleanupResult({
        category,
        message: `Deleted ${count} ${noun}${count === 1 ? "" : "s"}, freed ${fmtBytes(result.bytes_freed)}.`,
      });
    } finally {
      setCleaningUp(null);
    }
  }

  async function handleFlushAllData() {
    if (
      !confirm(
        `Delete ALL saved markers, cues, segments and snapshots for "${job?.name ?? "this channel"}"?\n\n` +
          "The channel itself and its settings (name, source, tuning, retention) are kept -- " +
          "only its collected SCTE-35 data is wiped. This cannot be undone.",
      )
    ) {
      return;
    }
    setFlushing(true);
    setFlushResult(null);
    try {
      const result = await api.flushJobData(jobId);
      setFlushResult(
        `Deleted ${result.markers_deleted} marker(s), ${result.cues_deleted} cue(s), ` +
          `${result.segments_deleted} segment(s), ${result.snapshot_files_deleted} snapshot file(s), ` +
          `freed ${fmtBytes(result.bytes_freed)}.`,
      );
      // Reflect the flush in the GUI immediately -- new markers/segments/
      // snapshots (if the job is still running) will still arrive live over
      // the websocket exactly as before, this just clears what's already
      // rendered instead of waiting on a reconnect/backlog replay.
      markerIds.current = new Set();
      segmentIds.current = new Set();
      cueSnapshotIds.current = new Set();
      setMarkers([]);
      setSegments([]);
      setCueSnapshots([]);
    } catch (e) {
      setFlushResult(e instanceof Error ? e.message : String(e));
    } finally {
      setFlushing(false);
    }
  }

  if (!job) {
    return <div className="p-8 text-sm text-slate-500">Loading…</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <button onClick={onBack} className="mb-2 text-sm text-slate-400 hover:text-slate-200">
            ← All jobs
          </button>
          <h2 className="text-xl font-semibold text-slate-100">{job.name}</h2>
          <div className="mt-1 flex items-center gap-2 text-sm text-slate-400">
            <Badge text={job.status} variant={jobStatusVariant(job.status)} />
            <span className={`h-2 w-2 rounded-full ${wsConnected ? "bg-emerald-500" : "bg-slate-600"}`} />
            <span className="text-xs">{wsConnected ? "Live" : "Disconnected"}</span>
          </div>
          {job.error && <p className="mt-1 text-sm text-red-400">{job.error}</p>}
        </div>
        <div className="flex gap-2">
          {job.is_running ? (
            <button
              onClick={() => api.stopJob(jobId)}
              className="rounded-md bg-amber-600/20 px-3 py-1.5 text-sm text-amber-300 ring-1 ring-amber-600/40 hover:bg-amber-600/30"
            >
              Stop job
            </button>
          ) : (
            <>
              <button
                onClick={() => onRestart(jobId)}
                className="rounded-md bg-emerald-600/20 px-3 py-1.5 text-sm text-emerald-300 ring-1 ring-emerald-600/40 hover:bg-emerald-600/30"
              >
                Restart
              </button>
              <button
                onClick={() => onEdit(jobId)}
                className="rounded-md bg-slate-800 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-700"
              >
                Edit
              </button>
            </>
          )}
          <a
            href={api.markersCsvUrl(jobId)}
            className="rounded-md bg-slate-800 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-700"
          >
            Export CSV
          </a>
          <div className="relative">
            <button
              onClick={() => setCleanupMenuOpen((o) => !o)}
              className="rounded-md bg-slate-800 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-700"
            >
              Clean up
            </button>
            {cleanupMenuOpen && (
              <>
                <div
                  className="fixed inset-0 z-10"
                  onClick={() => {
                    setCleanupMenuOpen(false);
                    setOpenCleanupCategory(null);
                  }}
                />
                <div className="absolute right-0 z-20 mt-1 w-80 rounded-md bg-slate-900 py-1 shadow-lg ring-1 ring-slate-700">
                  <CleanupMenuCategory
                    label="Segments (TS dump)"
                    expanded={openCleanupCategory === "segment"}
                    onToggle={() => setOpenCleanupCategory((c) => (c === "segment" ? null : "segment"))}
                    retentionS={job.segment_retention_s}
                    onRetentionChange={(v) => handleRetentionChange("segment", v)}
                    savingRetention={savingRetention === "segment"}
                    cleanupAgeS={cleanupAge.segment}
                    onCleanupAgeChange={(v) => setCleanupAge((a) => ({ ...a, segment: v }))}
                    onCleanupNow={() => handleCleanupNow("segment")}
                    cleaningUp={cleaningUp === "segment"}
                    resultMessage={cleanupResult?.category === "segment" ? cleanupResult.message : null}
                  />
                  <CleanupMenuCategory
                    label="Snapshots (images)"
                    expanded={openCleanupCategory === "snapshot"}
                    onToggle={() => setOpenCleanupCategory((c) => (c === "snapshot" ? null : "snapshot"))}
                    retentionS={job.snapshot_retention_s}
                    onRetentionChange={(v) => handleRetentionChange("snapshot", v)}
                    savingRetention={savingRetention === "snapshot"}
                    cleanupAgeS={cleanupAge.snapshot}
                    onCleanupAgeChange={(v) => setCleanupAge((a) => ({ ...a, snapshot: v }))}
                    onCleanupNow={() => handleCleanupNow("snapshot")}
                    cleaningUp={cleaningUp === "snapshot"}
                    resultMessage={cleanupResult?.category === "snapshot" ? cleanupResult.message : null}
                  />
                  <div className="border-t border-slate-800 px-3 py-2">
                    <button
                      onClick={handleFlushAllData}
                      disabled={flushing}
                      className="w-full rounded-md bg-red-500/10 px-2.5 py-1.5 text-left text-sm text-red-400 ring-1 ring-red-500/30 hover:bg-red-500/20 disabled:cursor-not-allowed disabled:opacity-50"
                      title="Deletes every marker, cue, segment and snapshot for this channel -- keeps its name, source and tuning settings"
                    >
                      {flushing ? "Flushing…" : "Flush all data"}
                    </button>
                    <p className="mt-1 text-[11px] leading-snug text-slate-500">
                      Wipes every marker/cue/segment/snapshot for this channel. Keeps the channel and its settings
                      (name, source, tuning, retention). Cannot be undone.
                    </p>
                    {flushResult && <p className="mt-1 text-xs text-emerald-400">{flushResult}</p>}
                  </div>
                  <p className="border-t border-slate-800 px-3 pt-2 text-[11px] leading-snug text-slate-500">
                    Auto-delete runs in the background (checked roughly every 15 minutes). "Clean up now" deletes
                    immediately and doesn't change that setting. Deleting old snapshots leaves older marker rows
                    without a thumbnail -- the marker data itself is unaffected.
                  </p>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {job.pid_info && (
        <div className="flex flex-wrap gap-6 rounded-lg bg-slate-900 px-4 py-3 text-xs text-slate-400 ring-1 ring-slate-800">
          <span>
            Video PID: <span className="mono text-slate-200">{fmtPid(job.pid_info.video_pid)}</span>
          </span>
          <span>
            Codec: <span className="mono text-slate-200">{job.pid_info.video_codec ?? "–"}</span>
          </span>
          <span>
            SCTE-35 PID: <span className="mono text-slate-200">{fmtPid(job.pid_info.scte35_pid)}</span>
          </span>
          <span>
            PMT PID: <span className="mono text-slate-200">{fmtPid(job.pid_info.pmt_pid)}</span>
          </span>
        </div>
      )}

      <section className="rounded-lg bg-slate-900 ring-1 ring-slate-800">
        <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
          <h3 className="text-sm font-medium text-slate-300">Video info</h3>
          <div className="flex items-center gap-2">
            {job.video_info?.sampled_at && (
              <span className="text-xs text-slate-500">Sampled {job.video_info.sampled_at} UTC</span>
            )}
            <button
              onClick={async () => {
                setRefreshingVideoInfo(true);
                try {
                  await api.refreshVideoInfo(jobId);
                } catch {
                  // Most likely the job isn't running -- nothing to refresh from.
                } finally {
                  setRefreshingVideoInfo(false);
                }
              }}
              disabled={!job.is_running || refreshingVideoInfo}
              title={job.is_running ? "Sample and ffprobe the stream again now" : "Only available while the job is running"}
              className="rounded-md bg-slate-800 px-2.5 py-1 text-xs text-slate-300 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {refreshingVideoInfo ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>
        {job.video_info ? (
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 p-4 text-xs text-slate-400 sm:grid-cols-3 lg:grid-cols-4">
            <VideoInfoField label="Resolution" value={fmtResolution(job.video_info.width, job.video_info.height)} />
            <VideoInfoField label="Codec" value={job.video_info.codec_name} />
            <VideoInfoField label="Profile / level" value={fmtProfileLevel(job.video_info.profile, job.video_info.level)} />
            <VideoInfoField label="Frame rate" value={fmtFrameRate(job.video_info.frame_rate_fps, job.video_info.r_frame_rate)} />
            <VideoInfoField label="Scan type" value={fmtScanType(job.video_info.scan_type, job.video_info.field_order)} />
            <VideoInfoField label="Pixel format" value={job.video_info.pix_fmt} />
            <VideoInfoField
              label="Color space"
              value={fmtColor(job.video_info.color_space, job.video_info.color_primaries, job.video_info.color_transfer)}
            />
            <VideoInfoField label="Color range" value={job.video_info.color_range} />
            <VideoInfoField
              label="GOP length (avg)"
              value={
                job.video_info.gop?.avg_gop_length_frames != null
                  ? `${job.video_info.gop.avg_gop_length_frames} frames`
                  : null
              }
            />
            {job.video_info.gop?.pattern && (
              <div className="col-span-full">
                <span className="text-slate-500">GOP pattern: </span>
                <span className="mono text-slate-200">{job.video_info.gop.pattern}</span>
              </div>
            )}
          </div>
        ) : (
          <p className="px-4 py-3 text-xs text-slate-500">
            {job.is_running
              ? "Waiting for the first sample (sampled periodically, roughly every 20s once the video PID is known)…"
              : "No sample captured yet -- start the job to get one, or Restart it."}
          </p>
        )}
      </section>

      <section className="rounded-lg bg-slate-900 p-4 ring-1 ring-slate-800">
        <h3 className="mb-2 text-sm font-medium text-slate-300">
          Delta (SCTE-35 target vs. matched IDR), over time (UTC)
        </h3>
        <DeltaChart key={jobId} markers={markers} okThresholdMs={Number(job.tuning_config.ok_threshold_ms ?? 41)} />
      </section>

      <section className="rounded-lg bg-slate-900 ring-1 ring-slate-800">
        <h3 className="border-b border-slate-800 px-4 py-3 text-sm font-medium text-slate-300">
          Markers ({markers.length})
        </h3>
        <div className="max-h-[520px] overflow-y-auto">
          <MarkerTable
            jobId={jobId}
            markers={markers}
            segments={segments}
            cueSnapshots={cueSnapshots}
            onOpenSegment={setPlayingSegment}
            onOpenImage={(src, caption) => setViewingImage({ src, caption })}
          />
        </div>
      </section>

      {playingSegment && (
        <SegmentPlayerModal jobId={jobId} segment={playingSegment} onClose={() => setPlayingSegment(null)} />
      )}
      {viewingImage && (
        <ImageLightbox
          src={viewingImage.src}
          caption={viewingImage.caption}
          onClose={() => setViewingImage(null)}
        />
      )}
    </div>
  );
}

/** One expandable row inside the "Clean up" menu (see the button next to
 * "Export CSV" above) -- a submenu, in effect: click the category name to
 * expand/collapse its controls in place, rather than a separate page or
 * section. Holds the exact same two controls the old standalone "Storage
 * cleanup" section had (auto-delete-after + one-off "Clean up now"), just
 * laid out for a narrow popover instead of a full-width row. */
function CleanupMenuCategory({
  label,
  expanded,
  onToggle,
  retentionS,
  onRetentionChange,
  savingRetention,
  cleanupAgeS,
  onCleanupAgeChange,
  onCleanupNow,
  cleaningUp,
  resultMessage,
}: {
  label: string;
  expanded: boolean;
  onToggle: () => void;
  retentionS: number | null;
  onRetentionChange: (valueS: number | null) => void;
  savingRetention: boolean;
  cleanupAgeS: number;
  onCleanupAgeChange: (valueS: number) => void;
  onCleanupNow: () => void;
  cleaningUp: boolean;
  resultMessage: string | null;
}) {
  return (
    <div className="border-b border-slate-800 last:border-b-0">
      <button
        onClick={onToggle}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-sm text-slate-200 hover:bg-slate-800"
      >
        {label}
        <span className="text-xs text-slate-500">{expanded ? "▲" : "▼"}</span>
      </button>
      {expanded && (
        <div className="space-y-2 bg-slate-950/40 px-3 pb-3 pt-1">
          <label className="flex items-center justify-between gap-2 text-xs text-slate-400">
            Auto-delete after
            <select
              value={String(retentionS ?? "null")}
              onChange={(e) => onRetentionChange(e.target.value === "null" ? null : Number(e.target.value))}
              disabled={savingRetention}
              className="rounded-md bg-slate-800 px-2 py-1 text-xs text-slate-200 ring-1 ring-slate-700 disabled:opacity-50"
            >
              {RETENTION_PRESETS.map((p) => (
                <option key={p.label} value={String(p.valueS ?? "null")}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <div className="flex items-center gap-2 text-xs text-slate-400">
            <select
              value={String(cleanupAgeS)}
              onChange={(e) => onCleanupAgeChange(Number(e.target.value))}
              disabled={cleaningUp}
              className="min-w-0 flex-1 rounded-md bg-slate-800 px-2 py-1 text-xs text-slate-200 ring-1 ring-slate-700 disabled:opacity-50"
            >
              {CLEANUP_AGE_PRESETS.map((p) => (
                <option key={p.label} value={String(p.valueS)}>
                  {p.label}
                </option>
              ))}
            </select>
            <button
              onClick={onCleanupNow}
              disabled={cleaningUp}
              className="shrink-0 rounded-md bg-slate-800 px-2.5 py-1 text-xs text-slate-300 hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {cleaningUp ? "Cleaning…" : "Clean up now"}
            </button>
          </div>
          {resultMessage && <p className="text-xs text-emerald-400">{resultMessage}</p>}
        </div>
      )}
    </div>
  );
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function fmtPid(pid: number | null | undefined): string {
  if (pid === null || pid === undefined) return "–";
  return `0x${pid.toString(16).toUpperCase()} (${pid})`;
}

function VideoInfoField({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div>
      <div className="text-slate-500">{label}</div>
      <div className="mono text-slate-200">{value || "–"}</div>
    </div>
  );
}

function fmtResolution(w: number | null, h: number | null): string | null {
  if (!w || !h) return null;
  return `${w}×${h}`;
}

function fmtProfileLevel(profile: string | null, level: number | null): string | null {
  if (!profile && level == null) return null;
  // ffprobe reports H.264/HEVC level as level*10 (e.g. 41 = Level 4.1).
  const levelStr = level != null ? (level / 10).toFixed(1) : null;
  return [profile, levelStr ? `L${levelStr}` : null].filter(Boolean).join(" ");
}

function fmtFrameRate(fps: number | null, raw: string | null): string | null {
  if (fps == null) return raw;
  return `${fps} fps`;
}

function fmtScanType(scanType: string | null, fieldOrder: string | null): string | null {
  if (!scanType || scanType === "unknown") return null;
  const label = scanType === "progressive" ? "Progressive" : "Interlaced";
  return fieldOrder && fieldOrder !== scanType ? `${label} (${fieldOrder})` : label;
}

function fmtColor(space: string | null, primaries: string | null, transfer: string | null): string | null {
  const parts = [space, primaries, transfer].filter((p) => p && p !== "unknown");
  return parts.length ? parts.join(" / ") : null;
}
