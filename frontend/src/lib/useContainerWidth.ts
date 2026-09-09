import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

/** Tracks a container element's actual on-screen width (CSS pixels) via
 * ResizeObserver, for an SVG chart whose viewBox needs to match its real
 * rendered size exactly.
 *
 * Why this matters: DeltaChart/CompareChart draw into a fixed-size viewBox
 * (e.g. "0 0 900 220") stretched to fill a responsive container (`w-full`)
 * via `preserveAspectRatio="none"`. Circle radii and font sizes are
 * authored in that SAME viewBox coordinate space as the plotted geometry
 * -- so when the container is wider than the viewBox's 900 units, the
 * browser scales EVERYTHING up to fill it, dots and text included, not
 * just the axes. Widen the browser window and the whole chart visibly
 * grows instead of just gaining more plotted width. Using the container's
 * real measured width as the viewBox width keeps that scale factor at a
 * constant 1:1 regardless of window size, so dot/text size stays fixed
 * and only the plotted X/Y extent actually stretches -- which is what a
 * "responsive chart" should mean.
 *
 * Returns a ref to attach to the measured container and its current
 * width; `defaultWidth` is only what's used for the very first render,
 * before the ResizeObserver has reported anything. */
export function useContainerWidth(defaultWidth: number): [RefObject<HTMLDivElement>, number] {
  // `useRef<HTMLDivElement>(null)` (T given explicitly, null passed in) is
  // the idiom that resolves to React's RefObject<HTMLDivElement> overload
  // -- whose own `current` field is already typed `HTMLDivElement | null`
  // -- rather than MutableRefObject<HTMLDivElement | null>, which is NOT
  // assignable to a JSX `ref` prop typed RefObject<HTMLDivElement>|
  // LegacyRef<HTMLDivElement> in this project's React type version.
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(defaultWidth);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w && w > 0) setWidth(w);
    });
    observer.observe(el);
    // Also measure synchronously on mount -- ResizeObserver's own first
    // callback already fires with the initial size, but doing it here too
    // avoids a one-frame flash at defaultWidth if the container's actual
    // width differs noticeably (e.g. a narrow sidebar-adjacent column).
    if (el.clientWidth > 0) setWidth(el.clientWidth);
    return () => observer.disconnect();
  }, []);

  return [ref, width];
}
