import { useEffect, useState } from "react";
import { formatLocalClock, formatUtcClock, localZoneName } from "../lib/time";

/** Live UTC + local clock for the header (see App.tsx) -- every timestamp
 * in this tool (markers, cues, segments) is stored and displayed in UTC,
 * so this exists to make "what time is it in that other zone right now"
 * a glance instead of mental math. Ticks every second; purely client-side,
 * no API call. */
export default function Clock() {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="hidden items-center gap-2.5 rounded-md bg-slate-900 px-3 py-1.5 text-xs ring-1 ring-slate-800 sm:flex">
      <span className="mono text-slate-300" title="Coordinated Universal Time -- what every timestamp in this tool uses">
        <span className="mr-1 text-slate-500">UTC</span>
        {formatUtcClock(now)}
      </span>
      <span className="h-3 w-px bg-slate-700" />
      <span className="mono text-slate-300" title={localZoneName()}>
        <span className="mr-1 text-slate-500">Local</span>
        {formatLocalClock(now)}
      </span>
    </div>
  );
}
