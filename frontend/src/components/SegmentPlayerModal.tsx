import { useState } from "react";
import type { Segment } from "../api/types";
import { api } from "../api/client";

export default function SegmentPlayerModal({
  jobId,
  segment,
  onClose,
}: {
  jobId: string;
  segment: Segment;
  onClose: () => void;
}) {
  const [playbackError, setPlaybackError] = useState<string | null>(null);

  // <video onError> only reports that loading failed, not why -- refetch
  // the same URL as plain JSON to surface the backend's actual error
  // (e.g. the ffmpeg remux failure text) instead of making the user open
  // devtools to see it.
  async function explainPlaybackError() {
    try {
      const res = await fetch(api.segmentMp4Url(jobId, segment.id));
      if (res.ok) return; // transient -- the element's own error was the real signal
      const body = await res.json().catch(() => null);
      setPlaybackError(body?.detail ?? `${res.status} ${res.statusText}`);
    } catch (e) {
      setPlaybackError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-3xl rounded-lg bg-slate-900 ring-1 ring-slate-700"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
          <div>
            <h3 className="font-medium text-slate-100">
              Clip: {segment.window_start_wallclock} – {segment.window_end_wallclock}
            </h3>
            <p className="text-xs text-slate-400">
              {segment.event_count} SCTE-35 event(s) · cue_seq {segment.event_cue_seqs.join(", ")} ·{" "}
              {(segment.size_bytes / 1e6).toFixed(1)} MB
              {segment.included_previous_window ? " · includes the previous window as pre-roll" : ""}
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
          >
            ✕
          </button>
        </div>
        <div className="bg-black">
          <video
            key={segment.id}
            src={api.segmentMp4Url(jobId, segment.id)}
            controls
            autoPlay
            className="max-h-[70vh] w-full"
            onError={explainPlaybackError}
          />
        </div>
        {playbackError && (
          <div className="mx-4 mt-3 rounded-md bg-red-500/10 px-3 py-2 text-xs text-red-400 ring-1 ring-red-500/30">
            Playback failed: {playbackError}
          </div>
        )}
        <div className="flex justify-end gap-2 px-4 py-3">
          <a
            href={api.segmentTsUrl(jobId, segment.id)}
            className="rounded bg-slate-800 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700"
            download
          >
            Download raw .ts
          </a>
        </div>
      </div>
    </div>
  );
}
