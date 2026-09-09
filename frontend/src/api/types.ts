export type SourceType = "file" | "udp" | "hls" | "dash";

export type JobStatus = "starting" | "running" | "finished" | "stopped" | "error";

export interface PidInfo {
  pmt_pid?: number | null;
  video_pid?: number | null;
  video_codec?: string | null;
  scte35_pid?: number | null;
}

/** GOP structure derived from a short run of decoded frame types -- see
 * VideoInfo.gop. avg_gop_length_frames/pattern are null if the sample
 * didn't contain two full key-frame-to-key-frame cycles. */
export interface VideoInfoGop {
  frames_sampled: number;
  keyframes_sampled: number;
  avg_gop_length_frames: number | null;
  pattern: string;
}

/** Detailed technical stream info from a periodic ffprobe sample of the
 * raw TS (see app/core/probe.py's Probe._sample_video_info) -- distinct
 * from PidInfo (which comes from this tool's own PAT/PMT parsing, present
 * from the first packet). Any field can be null if ffprobe's sample
 * didn't carry it (e.g. color_* fields are commonly unset on SD content
 * with no VUI parameters). */
export interface VideoInfo {
  sampled_at: string;
  codec_name: string | null;
  profile: string | null;
  level: number | null;
  width: number | null;
  height: number | null;
  pix_fmt: string | null;
  color_range: string | null;
  color_space: string | null;
  color_transfer: string | null;
  color_primaries: string | null;
  field_order: string | null;
  scan_type: "progressive" | "interlaced" | "unknown";
  r_frame_rate: string | null;
  avg_frame_rate: string | null;
  frame_rate_fps: number | null;
  gop: VideoInfoGop | null;
}

export interface Job {
  id: string;
  name: string;
  source_type: SourceType;
  source_config: Record<string, unknown>;
  tuning_config: Record<string, unknown>;
  status: JobStatus;
  error: string | null;
  pid_info: PidInfo | null;
  video_info: VideoInfo | null;
  /** Seconds; null = "keep forever" (the default -- nothing is auto-deleted
   * unless a limit is set). Enforced by a background sweep roughly every
   * 15 minutes, independent of whether the job is running -- see the
   * "Storage cleanup" panel on the job detail page. */
  segment_retention_s: number | null;
  snapshot_retention_s: number | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  is_running: boolean;
}

export interface Marker {
  id: number;
  job_id: string;
  cue_seq: number | null;
  event_id: string | null;
  command_type: string | null;
  out_of_network: boolean | null;
  target_pts_s: number | null;
  raw_pts_time_s: number | null;
  pts_adjustment_ticks: number | null;
  idr_pts_s: number | null;
  delta_ms: number | null;
  verdict: string | null;
  codec: string | null;
  au_kind: string | null;
  segmentation_summary: string[];
  snapshot_path: string | null;
  pre_frame_snapshot_paths: string[];
  time_to_event_ms: number | null;
  actual_preroll_ms: number | null;
  preroll_delta_ms: number | null;
  preroll_verdict: string | null;
  signal_verdict: string | null;
  gop_verdict: string | null;
  wallclock: string | null;
}

export interface Cue {
  id: number;
  job_id: string;
  cue_seq: number | null;
  wallclock: string | null;
  command_type: string | null;
  descriptor_summary: string[];
  section_hex: string | null;
  cue: unknown;
}

/** A reference-frame snapshot: "which exact frame does time_to_event / the
 * pre-roll deadline point to?" -- see TuningConfig's time_to_event_snapshot
 * / preroll_snapshot fields for the four capture modes this can come from. */
export interface CueSnapshot {
  id: number;
  job_id: string;
  cue_seq: number | null;
  tag:
    | "time_to_event_cue_arrival"
    | "time_to_event_target_pts"
    | "preroll_realtime_deadline"
    | "preroll_same_as_matched_idr";
  path: string;
  frame_pts_s: number | null;
  created_at: string;
}

export interface Segment {
  id: number;
  job_id: string;
  path: string;
  mp4_path: string | null;
  window_start_wallclock: string | null;
  window_end_wallclock: string | null;
  window_s: number | null;
  included_previous_window: boolean;
  event_count: number;
  event_cue_seqs: number[];
  size_bytes: number;
}

export interface TuningConfig {
  program: number | null;
  pid_video: string | null;
  pid_scte35: string | null;
  codec: "h264" | "hevc" | null;
  tolerance_ms: number;
  max_early_ms: number;
  timeout_s: number;
  ok_threshold_ms: number;
  preroll_tolerance_ms: number;
  min_time_to_event_ms: number;
  include_cra: boolean;
  snapshot_enabled: boolean;
  snapshot_all_idr: boolean;
  pre_frames: number;
  time_to_event_snapshot: "off" | "cue_arrival_frame" | "target_pts_frame";
  preroll_snapshot: "off" | "realtime_deadline_frame" | "same_as_matched_idr";
  segment_save_enabled: boolean;
  segment_window_s: number;
  segment_no_preroll: boolean;
  segment_max_files: number | null;
}

export const DEFAULT_TUNING: TuningConfig = {
  program: null,
  pid_video: null,
  pid_scte35: null,
  codec: null,
  tolerance_ms: 6000,
  max_early_ms: 50,
  timeout_s: 12,
  ok_threshold_ms: 41,
  preroll_tolerance_ms: 500,
  min_time_to_event_ms: 4000,
  include_cra: false,
  snapshot_enabled: true,
  snapshot_all_idr: false,
  pre_frames: 0,
  time_to_event_snapshot: "off",
  preroll_snapshot: "off",
  segment_save_enabled: true,
  segment_window_s: 60,
  segment_no_preroll: false,
  segment_max_files: 500,
};

/** Auto-delete-after choices for the "Storage cleanup" panel, in seconds --
 * mirrors backend app/jobs/cleanup.py's RETENTION_PRESETS_S (kept as a
 * separate literal here since the frontend also needs the "Never" (null)
 * option and human-readable labels, which that dict doesn't carry). */
export const RETENTION_PRESETS: { label: string; valueS: number | null }[] = [
  { label: "Never", valueS: null },
  { label: "1 hour", valueS: 3600 },
  { label: "6 hours", valueS: 6 * 3600 },
  { label: "12 hours", valueS: 12 * 3600 },
  { label: "1 day", valueS: 24 * 3600 },
  { label: "1 week", valueS: 7 * 24 * 3600 },
];

/** Age choices for the one-off "Clean up now" action -- same thresholds as
 * RETENTION_PRESETS minus "Never" (an immediate purge needs an actual age),
 * plus "Everything", which the backend treats as max_age_s=0. */
export const CLEANUP_AGE_PRESETS: { label: string; valueS: number }[] = [
  { label: "Older than 1 hour", valueS: 3600 },
  { label: "Older than 6 hours", valueS: 6 * 3600 },
  { label: "Older than 12 hours", valueS: 12 * 3600 },
  { label: "Older than 1 day", valueS: 24 * 3600 },
  { label: "Older than 1 week", valueS: 7 * 24 * 3600 },
  { label: "Everything", valueS: 0 },
];

export type WsMessage =
  | { kind: "pat_info"; data: { program_number: number; pmt_pid: number }; backlog?: boolean }
  | { kind: "pid_info"; data: PidInfo; backlog?: boolean }
  | { kind: "video_info"; data: VideoInfo; backlog?: boolean }
  | { kind: "scte35_cue"; data: Cue; backlog?: boolean }
  | { kind: "match_result"; data: Marker; backlog?: boolean }
  | { kind: "snapshot_saved"; data: { path: string; kind: string }; backlog?: boolean }
  | { kind: "reference_snapshot_saved"; data: CueSnapshot; backlog?: boolean }
  | { kind: "segment_saved"; data: Segment; backlog?: boolean }
  | { kind: "status"; data: { status: JobStatus; message?: string | null }; backlog?: boolean };
