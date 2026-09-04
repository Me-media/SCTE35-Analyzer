import { useState, type ReactNode } from "react";
import { api, type LiveSourceInput } from "../api/client";
import { DEFAULT_TUNING, type Job, type TuningConfig } from "../api/types";

type Tab = "file" | "udp" | "hls" | "dash";

export default function NewJobForm({
  onCreated,
  editJob,
  onSaved,
}: {
  onCreated: (jobId: string) => void;
  // When set, the form edits this existing (non-running) job instead of
  // creating a new one: the source-type tab is locked to the job's own
  // source_type (a "file" job's uploaded file can't be swapped this way --
  // create a new job for that instead), all fields are pre-filled from it,
  // and submitting calls PATCH /api/jobs/{id} instead of a create endpoint.
  editJob?: Job;
  onSaved?: () => void;
}) {
  const editSource = editJob?.source_config as Record<string, unknown> | undefined;
  const [subTab, setSubTab] = useState<Tab>((editJob?.source_type as Tab) ?? "udp");
  const [name, setName] = useState(editJob?.name ?? "");
  const [file, setFile] = useState<File | null>(null);
  const [addr, setAddr] = useState((editSource?.addr as string) ?? "239.1.1.1");
  const [port, setPort] = useState((editSource?.port as number) ?? 5000);
  const [iface, setIface] = useState((editSource?.iface as string) ?? "");
  const [transport, setTransport] = useState<"auto" | "ts" | "rtp">(
    (editSource?.transport as "auto" | "ts" | "rtp") ?? "auto",
  );
  const [url, setUrl] = useState((editSource?.url as string) ?? "");
  const [tuning, setTuning] = useState<TuningConfig>(
    editJob ? { ...DEFAULT_TUNING, ...(editJob.tuning_config as Partial<TuningConfig>) } : DEFAULT_TUNING,
  );
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const patch = (partial: Partial<TuningConfig>) => setTuning((t) => ({ ...t, ...partial }));

  async function submit() {
    setError(null);
    const jobName = name.trim() || defaultName(subTab, { addr, port, url, file });
    setSubmitting(true);
    try {
      if (editJob) {
        const body: Parameters<typeof api.updateJob>[1] = { name: jobName, tuning };
        if (editJob.source_type !== "file") {
          if (subTab === "udp") body.source = { type: "udp", addr, port, iface: iface || undefined, transport };
          else if (subTab === "hls") body.source = { type: "hls", url };
          else if (subTab === "dash") body.source = { type: "dash", url };
        }
        await api.updateJob(editJob.id, body);
        onSaved?.();
        return;
      }
      let jobId: string;
      if (subTab === "file") {
        if (!file) throw new Error("Choose an MPEG-TS file to upload");
        const res = await api.createUploadJob(jobName, file, tuning);
        jobId = res.job_id;
      } else {
        let source: LiveSourceInput;
        if (subTab === "udp") {
          source = { type: "udp", addr, port, iface: iface || undefined, transport };
        } else if (subTab === "hls") {
          source = { type: "hls", url };
        } else {
          source = { type: "dash", url };
        }
        const res = await api.createLiveJob(jobName, source, tuning);
        jobId = res.job_id;
      }
      onCreated(jobId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <label className="mb-1 block text-sm text-slate-400">Job name{editJob ? "" : " (optional)"}</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={defaultName(subTab, { addr, port, url, file })}
          className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-sm outline-none focus:border-sky-500"
        />
      </div>

      {editJob ? (
        <p className="text-xs text-slate-500">
          Source type: <span className="text-slate-300">{editJob.source_type.toUpperCase()}</span> (can't be
          changed here -- create a new job for a different source type or file)
        </p>
      ) : (
        <div className="flex gap-1 rounded-lg bg-slate-900 p-1 ring-1 ring-slate-800">
          {(
            [
              ["file", "Upload file"],
              ["udp", "Multicast UDP/RTP"],
              ["hls", "HLS"],
              ["dash", "DASH"],
            ] as [Tab, string][]
          ).map(([t, label]) => (
            <button
              key={t}
              onClick={() => setSubTab(t)}
              className={`flex-1 rounded-md px-3 py-2 text-sm transition ${
                subTab === t ? "bg-sky-600 text-white" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      {subTab === "file" && !editJob && (
        <div className="space-y-2">
          <label className="block text-sm text-slate-400">MPEG-TS file (.ts)</label>
          <input
            type="file"
            accept=".ts,video/mp2t,application/octet-stream"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="block w-full text-sm text-slate-300 file:mr-3 file:rounded-md file:border-0 file:bg-slate-800 file:px-3 file:py-2 file:text-slate-200 hover:file:bg-slate-700"
          />
          <p className="text-xs text-slate-500">
            The file is read once, start to finish, through the same analysis engine as a live stream.
          </p>
        </div>
      )}
      {subTab === "file" && editJob && (
        <div className="space-y-1">
          <label className="block text-sm text-slate-400">MPEG-TS file</label>
          <p className="rounded-md bg-slate-900 px-3 py-2 text-sm text-slate-300 ring-1 ring-slate-800">
            {(editSource?.filename as string) ?? "(uploaded file)"}
          </p>
          <p className="text-xs text-slate-500">
            The uploaded file itself can't be swapped here -- create a new job to analyze a different file.
          </p>
        </div>
      )}

      {subTab === "udp" && (
        <div className="grid grid-cols-2 gap-3">
          <Field label="Address (multicast or unicast/loopback)">
            <input
              value={addr}
              onChange={(e) => setAddr(e.target.value)}
              className="input"
            />
          </Field>
          <Field label="Port">
            <input
              type="number"
              value={port}
              onChange={(e) => setPort(Number(e.target.value))}
              className="input"
            />
          </Field>
          <Field label="Interface (optional)">
            <input value={iface} onChange={(e) => setIface(e.target.value)} className="input" />
          </Field>
          <Field label="Transport">
            <select
              value={transport}
              onChange={(e) => setTransport(e.target.value as "auto" | "ts" | "rtp")}
              className="input"
            >
              <option value="auto">Auto-detect</option>
              <option value="ts">Raw MPEG-TS over UDP</option>
              <option value="rtp">RTP-encapsulated</option>
            </select>
          </Field>
          <p className="col-span-2 text-xs text-slate-500">
            224.0.0.0–239.255.255.255 is joined as a real multicast group. Anything else (e.g. 127.0.0.1) is
            listened to as plain unicast UDP, handy for local testing.
          </p>
        </div>
      )}

      {(subTab === "hls" || subTab === "dash") && (
        <div className="space-y-2">
          <Field label={subTab === "hls" ? "HLS URL (.m3u8)" : "DASH URL (.mpd)"}>
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder={subTab === "hls" ? "https://.../index.m3u8" : "https://.../manifest.mpd"}
              className="input"
            />
          </Field>
          <p className="text-xs text-slate-500">
            The stream is relayed through ffmpeg (remux only, no re-encode) to a local UDP listener. Intended
            for LIVE monitoring — for VOD, download it and use file upload instead.
          </p>
        </div>
      )}

      <div className="rounded-lg ring-1 ring-slate-800">
        <button
          onClick={() => setAdvancedOpen((v) => !v)}
          className="flex w-full items-center justify-between px-4 py-3 text-sm text-slate-300"
        >
          <span>Advanced tuning (PIDs, tolerances, snapshots, clip saving)</span>
          <span className="text-slate-500">{advancedOpen ? "▲" : "▼"}</span>
        </button>
        {advancedOpen && (
          <div className="grid grid-cols-2 gap-3 border-t border-slate-800 p-4">
            <Field label="Program number">
              <input
                type="number"
                value={tuning.program ?? ""}
                onChange={(e) => patch({ program: e.target.value ? Number(e.target.value) : null })}
                className="input"
              />
            </Field>
            <Field label="Codec (auto if empty)">
              <select
                value={tuning.codec ?? ""}
                onChange={(e) => patch({ codec: (e.target.value || null) as TuningConfig["codec"] })}
                className="input"
              >
                <option value="">Auto</option>
                <option value="h264">H.264</option>
                <option value="hevc">HEVC</option>
              </select>
            </Field>
            <Field label="Video PID (e.g. 0x101, auto if empty)">
              <input
                value={tuning.pid_video ?? ""}
                onChange={(e) => patch({ pid_video: e.target.value || null })}
                className="input"
              />
            </Field>
            <Field label="SCTE-35 PID (e.g. 0x1F0, auto if empty)">
              <input
                value={tuning.pid_scte35 ?? ""}
                onChange={(e) => patch({ pid_scte35: e.target.value || null })}
                className="input"
              />
            </Field>
            <NumberField label="Tolerance after target (ms)" value={tuning.tolerance_ms} onChange={(v) => patch({ tolerance_ms: v })} />
            <NumberField label="Max early IDR (ms)" value={tuning.max_early_ms} onChange={(v) => patch({ max_early_ms: v })} />
            <NumberField label="Timeout before MISSED (s)" value={tuning.timeout_s} onChange={(v) => patch({ timeout_s: v })} />
            <NumberField label="OK threshold (ms)" value={tuning.ok_threshold_ms} onChange={(v) => patch({ ok_threshold_ms: v })} />
            <NumberField
              label="Pre-roll tolerance (ms)"
              value={tuning.preroll_tolerance_ms}
              onChange={(v) => patch({ preroll_tolerance_ms: v })}
            />
            <NumberField
              label="Min. advance-notice time (ms)"
              value={tuning.min_time_to_event_ms}
              onChange={(v) => patch({ min_time_to_event_ms: v })}
            />
            <Toggle label="Include HEVC CRA as a splice point" checked={tuning.include_cra} onChange={(v) => patch({ include_cra: v })} />
            <div />
            <Toggle label="Save a JPEG snapshot of the matched IDR" checked={tuning.snapshot_enabled} onChange={(v) => patch({ snapshot_enabled: v })} />
            <Toggle
              label="Snapshot ALL IDRs (not just splice-relevant ones)"
              checked={tuning.snapshot_all_idr}
              onChange={(v) => patch({ snapshot_all_idr: v })}
            />
            <NumberField label="Pre-frames before the IDR" value={tuning.pre_frames} onChange={(v) => patch({ pre_frames: v })} />
            <div />
            <Field label="Time-to-event reference snapshot">
              <select
                value={tuning.time_to_event_snapshot}
                onChange={(e) => patch({ time_to_event_snapshot: e.target.value as TuningConfig["time_to_event_snapshot"] })}
                className="input"
              >
                <option value="off">Off</option>
                <option value="cue_arrival_frame">Frame on air when the cue arrived</option>
                <option value="target_pts_frame">Frame at the literal target PTS</option>
              </select>
            </Field>
            <Field label="Pre-roll reference snapshot">
              <select
                value={tuning.preroll_snapshot}
                onChange={(e) => patch({ preroll_snapshot: e.target.value as TuningConfig["preroll_snapshot"] })}
                className="input"
              >
                <option value="off">Off</option>
                <option value="realtime_deadline_frame">Frame at the real-time pre-roll deadline</option>
                <option value="same_as_matched_idr">Same frame as the matched IDR snapshot</option>
              </select>
            </Field>
            <p className="col-span-2 text-xs text-slate-500">
              These capture exactly which video frame time_to_event_ms / the pre-roll deadline
              refer to, separately from the matched-IDR snapshot above. "Same frame as the
              matched IDR snapshot" requires that IDR snapshot to also be enabled.
            </p>
            <Toggle
              label="Save clips (raw TS) around SCTE-35 events"
              checked={tuning.segment_save_enabled}
              onChange={(v) => patch({ segment_save_enabled: v })}
            />
            <NumberField
              label="Clip window (s)"
              value={tuning.segment_window_s}
              onChange={(v) => patch({ segment_window_s: v })}
            />
            <Toggle
              label="Exclude the previous window as pre-roll"
              checked={tuning.segment_no_preroll}
              onChange={(v) => patch({ segment_no_preroll: v })}
            />
            <NumberField
              label="Max saved clips"
              value={tuning.segment_max_files ?? 0}
              onChange={(v) => patch({ segment_max_files: v || null })}
            />
          </div>
        )}
      </div>

      {error && <div className="rounded-md bg-red-500/10 px-3 py-2 text-sm text-red-400 ring-1 ring-red-500/30">{error}</div>}

      <button
        onClick={submit}
        disabled={submitting}
        className="w-full rounded-md bg-sky-600 py-2.5 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-50"
      >
        {editJob ? (submitting ? "Saving…" : "Save changes") : submitting ? "Starting…" : "Start analysis"}
      </button>
    </div>
  );
}

function defaultName(
  tab: Tab,
  { addr, port, url, file }: { addr: string; port: number; url: string; file: File | null },
): string {
  if (tab === "file") return file?.name ?? "TS file";
  if (tab === "udp") return `${addr}:${port}`;
  return url || tab.toUpperCase();
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-slate-400">{label}</span>
      {children}
    </label>
  );
}

function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (v: number) => void }) {
  return (
    <Field label={label}>
      <input
        type="number"
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="input"
      />
    </Field>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 text-sm text-slate-300">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="h-4 w-4 rounded border-slate-600 bg-slate-800" />
      {label}
    </label>
  );
}
