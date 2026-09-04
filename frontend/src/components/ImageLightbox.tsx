/** Full-size view of a snapshot thumbnail (matched-IDR, pre-frame, or
 * time-to-event/pre-roll reference snapshot) -- click any thumbnail in
 * MarkerTable to open one of these instead of squinting at a 64x40px img. */
export default function ImageLightbox({
  src,
  caption,
  onClose,
}: {
  src: string;
  caption?: string;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-full max-w-full flex-col items-center gap-3"
        onClick={(e) => e.stopPropagation()}
      >
        <img
          src={src}
          alt={caption ?? "Snapshot"}
          className="max-h-[80vh] max-w-full rounded object-contain ring-1 ring-slate-700"
        />
        <div className="flex items-center gap-3">
          {caption && <span className="text-sm text-slate-300">{caption}</span>}
          <a
            href={src}
            download
            className="rounded-md bg-slate-800 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700"
          >
            Download
          </a>
          <button
            onClick={onClose}
            className="rounded-md bg-slate-800 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-700"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
