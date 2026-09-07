import type { Cue, CueSnapshot, Job, Marker, Segment, TuningConfig, WsMessage } from "./types";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // response body wasn't JSON -- keep statusText
    }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json() as Promise<T>;
}

export interface UdpSourceInput {
  type: "udp";
  addr: string;
  port: number;
  iface?: string;
  transport: "auto" | "ts" | "rtp";
}
export interface HlsSourceInput {
  type: "hls";
  url: string;
}
export interface DashSourceInput {
  type: "dash";
  url: string;
}
export type LiveSourceInput = UdpSourceInput | HlsSourceInput | DashSourceInput;

export interface UpdateJobInput {
  name?: string;
  source?: LiveSourceInput;
  tuning?: TuningConfig;
}

export const api = {
  getVersion: () => req<{ version: string }>("/api/version"),
  listJobs: () => req<Job[]>("/api/jobs"),
  getJob: (id: string) => req<Job>(`/api/jobs/${id}`),
  stopJob: (id: string) => req<{ ok: boolean }>(`/api/jobs/${id}/stop`, { method: "POST" }),
  // Re-runs a stopped/finished/errored job against its existing
  // source_config -- re-attaches to the same live source, or reprocesses
  // the same uploaded file. Markers/cues/snapshots/clips already
  // collected are kept; the new run's events are appended, never cleared.
  restartJob: (id: string) => req<{ ok: boolean }>(`/api/jobs/${id}/restart`, { method: "POST" }),
  // Asks a running job to re-sample+ffprobe the video stream right now
  // instead of waiting for the next periodic sample (every 20s). 409s if
  // the job isn't currently running.
  refreshVideoInfo: (id: string) =>
    req<{ ok: boolean }>(`/api/jobs/${id}/refresh_video_info`, { method: "POST" }),
  // Only valid while the job isn't running (backend returns 409 otherwise).
  // A "file" job's source can't be changed this way -- create a new job.
  updateJob: (id: string, body: UpdateJobInput) =>
    req<Job>(`/api/jobs/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteJob: (id: string) => req<{ ok: boolean }>(`/api/jobs/${id}`, { method: "DELETE" }),

  // How long this job's saved segments/snapshots are kept before the
  // background sweep deletes them (null = keep forever). Unlike updateJob
  // above, this is NOT gated on the job being stopped -- retention never
  // touches a running Probe.
  updateRetention: (
    id: string,
    body: { segment_retention_s: number | null; snapshot_retention_s: number | null },
  ) =>
    req<Job>(`/api/jobs/${id}/retention`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  // Immediate, one-off purge -- independent of the saved retention setting
  // above, and works regardless of whether the job is currently running.
  // Omit a field to skip that category.
  cleanupJob: (
    id: string,
    body: { segment_max_age_s?: number; snapshot_max_age_s?: number },
  ) =>
    req<{ segments_deleted: number; snapshot_files_deleted: number; bytes_freed: number }>(
      `/api/jobs/${id}/cleanup`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  // "Flush all data": deletes every marker/cue/segment/snapshot for this
  // job, but keeps the job itself (name/source/tuning/retention settings)
  // intact. No body -- unlike cleanupJob above, this always clears
  // everything, there's no partial/age-based variant of a deliberate wipe.
  flushJobData: (id: string) =>
    req<{
      markers_deleted: number;
      cues_deleted: number;
      segments_deleted: number;
      snapshot_files_deleted: number;
      bytes_freed: number;
    }>(`/api/jobs/${id}/flush`, { method: "POST" }),

  createLiveJob: (name: string, source: LiveSourceInput, tuning: TuningConfig) =>
    req<{ job_id: string }>("/api/jobs/live", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, source, tuning }),
    }),

  createUploadJob: async (name: string, file: File, tuning: TuningConfig) => {
    const form = new FormData();
    form.append("file", file);
    form.append("name", name);
    form.append("tuning", JSON.stringify(tuning));
    return req<{ job_id: string }>("/api/jobs/upload", { method: "POST", body: form });
  },

  listMarkers: (jobId: string, sinceId = 0) =>
    req<Marker[]>(`/api/jobs/${jobId}/markers?since_id=${sinceId}&limit=5000`),
  listCues: (jobId: string, sinceId = 0) =>
    req<Cue[]>(`/api/jobs/${jobId}/cues?since_id=${sinceId}&limit=1000`),
  listSegments: (jobId: string, sinceId = 0) =>
    req<Segment[]>(`/api/jobs/${jobId}/segments?since_id=${sinceId}&limit=500`),
  listCueSnapshots: (jobId: string, sinceId = 0) =>
    req<CueSnapshot[]>(`/api/jobs/${jobId}/cue_snapshots?since_id=${sinceId}&limit=1000`),

  markersCsvUrl: (jobId: string) => `/api/jobs/${jobId}/markers.csv`,
  snapshotUrl: (jobId: string, filename: string) =>
    `/api/media/snapshots/${jobId}/${encodeURIComponent(filename)}`,
  segmentMp4Url: (jobId: string, segmentId: number) =>
    `/api/media/segments/${jobId}/${segmentId}.mp4`,
  segmentTsUrl: (jobId: string, segmentId: number) =>
    `/api/media/segments/${jobId}/${segmentId}.ts`,
};

/** Basename of a server-side path (snapshot_path/segment path are absolute
 * container paths) -- used to build the /api/media/... URLs above. */
export function basename(path: string | null | undefined): string {
  if (!path) return "";
  const parts = path.split("/");
  return parts[parts.length - 1];
}

export function connectJobSocket(
  jobId: string,
  onMessage: (msg: WsMessage) => void,
  onStateChange?: (connected: boolean) => void,
): () => void {
  let closedByCaller = false;
  let socket: WebSocket | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let retryDelay = 1000;

  const connect = () => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${proto}://${window.location.host}/ws/jobs/${jobId}`);
    socket.onopen = () => {
      retryDelay = 1000;
      onStateChange?.(true);
    };
    socket.onmessage = (ev) => {
      try {
        onMessage(JSON.parse(ev.data) as WsMessage);
      } catch {
        // ignore malformed frame
      }
    };
    socket.onclose = () => {
      onStateChange?.(false);
      if (closedByCaller) return;
      retryTimer = setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 1.5, 10000);
    };
    socket.onerror = () => {
      socket?.close();
    };
  };

  connect();

  return () => {
    closedByCaller = true;
    if (retryTimer) clearTimeout(retryTimer);
    socket?.close();
  };
}
