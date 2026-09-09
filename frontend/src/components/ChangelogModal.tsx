import { useEffect, useState } from "react";
import { api } from "../api/client";

// A small, deliberately non-general markdown renderer -- just enough to
// render THIS project's own CHANGELOG.md (headings, possibly-nested bullet
// lists with line-wrapped/multi-paragraph items, and bold/code/italic/link
// inline spans) reasonably, not arbitrary markdown. No new npm dependency
// for something this narrow in scope. Verified against the real
// CHANGELOG.md content (all 0.1.0-0.11.0 entries, including the nested
// gop_verdict list with a trailing paragraph after its sub-list) before
// being wired in here. Degrades gracefully: anything that doesn't match a
// known pattern just falls through to a plain paragraph instead of being
// dropped or throwing.

type Block =
  | { type: "heading"; level: number; text: string }
  | { type: "paragraph"; text: string }
  | { type: "list"; items: ListItem[] };

// A list item's content in original document order -- NOT split into
// separate "paragraphs" and "subItems" buckets rendered one after the
// other, because that would silently reorder content whenever a paragraph
// follows a nested sub-list (as CHANGELOG.md's gop_verdict entry does: a
// paragraph, then a FORCED/GOP_WAIT/UNCLEAR/N-A sub-list, then ANOTHER
// paragraph -- rendering "all paragraphs, then the sub-list" would put
// that trailing paragraph before the sub-list it's meant to follow).
type ListItemPart = { type: "p"; text: string } | { type: "sublist"; items: string[] };
interface ListItem {
  parts: ListItemPart[];
}

function indentOf(line: string): number {
  return line.match(/^(\s*)/)?.[1].length ?? 0;
}
function headingMatch(line: string): RegExpMatchArray | null {
  return line.match(/^(#{1,6})\s+(.*)$/);
}
function bulletMatch(line: string): RegExpMatchArray | null {
  return line.match(/^(\s*)-\s+(.*)$/);
}

function parseMarkdown(src: string): Block[] {
  const lines = src.split("\n");
  const blocks: Block[] = [];
  let i = 0;

  function nextNonBlank(from: number): string | null {
    let j = from;
    while (j < lines.length && lines[j].trim() === "") j++;
    return j < lines.length ? lines[j] : null;
  }

  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === "") {
      i++;
      continue;
    }
    const h = headingMatch(line);
    if (h) {
      blocks.push({ type: "heading", level: h[1].length, text: h[2].trim() });
      i++;
      continue;
    }
    const b = bulletMatch(line);
    if (b && indentOf(line) === 0) {
      const items: ListItem[] = [{ parts: [{ type: "p", text: b[2].trim() }] }];
      i++;
      while (i < lines.length) {
        const l = lines[i];
        const curItem = items[items.length - 1];
        const lastPart = curItem.parts[curItem.parts.length - 1];
        if (l.trim() === "") {
          const peek = nextNonBlank(i + 1);
          if (peek === null || indentOf(peek) === 0) {
            i++;
            break; // end of file, or dedent to col 0 -- list ends here
          }
          // still indented after the blank -- starts a NEW paragraph part
          // within the current top-level item, in document order (so a
          // paragraph after a sub-list renders after it, not before)
          curItem.parts.push({ type: "p", text: "" });
          i++;
          continue;
        }
        const bm = bulletMatch(l);
        const depth = indentOf(l);
        if (bm && depth === 0) {
          items.push({ parts: [{ type: "p", text: bm[2].trim() }] });
          i++;
          continue;
        }
        if (bm && depth >= 2) {
          // a nested bullet right after another one (no blank line, no
          // paragraph break) extends the SAME sub-list; otherwise it opens
          // a new sub-list part at this point in the item's content
          if (lastPart.type === "sublist") lastPart.items.push(bm[2].trim());
          else curItem.parts.push({ type: "sublist", items: [bm[2].trim()] });
          i++;
          continue;
        }
        if (depth >= 2) {
          // continuation line-wrap: extend whichever part is currently active
          if (lastPart.type === "sublist") {
            const k = lastPart.items.length - 1;
            lastPart.items[k] = `${lastPart.items[k]} ${l.trim()}`.trim();
          } else {
            lastPart.text = `${lastPart.text} ${l.trim()}`.trim();
          }
          i++;
          continue;
        }
        break; // dedent to col 0, not a bullet -- bail out of the list
      }
      for (const it of items) it.parts = it.parts.filter((p) => p.type !== "p" || p.text !== "");
      blocks.push({ type: "list", items });
      continue;
    }
    const paraLines = [line.trim()];
    i++;
    while (i < lines.length) {
      const l = lines[i];
      if (l.trim() === "" || headingMatch(l)) break;
      const bm2 = bulletMatch(l);
      if (bm2 && indentOf(l) === 0) break;
      paraLines.push(l.trim());
      i++;
    }
    blocks.push({ type: "paragraph", text: paraLines.join(" ") });
  }
  return blocks;
}

const INLINE_RE = /(\*\*(.+?)\*\*)|(`([^`]+)`)|(\[([^\]]+)\]\(([^)]+)\))|(\*([^*]+)\*)/g;

function renderInline(text: string, keyPrefix: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  let n = 0;
  INLINE_RE.lastIndex = 0;
  while ((m = INLINE_RE.exec(text)) !== null) {
    if (m.index > lastIndex) out.push(text.slice(lastIndex, m.index));
    const key = `${keyPrefix}-${n++}`;
    if (m[1] !== undefined) {
      out.push(
        <strong key={key} className="font-semibold text-slate-100">
          {m[2]}
        </strong>,
      );
    } else if (m[3] !== undefined) {
      out.push(
        <code key={key} className="rounded bg-slate-800 px-1 py-0.5 text-[0.85em] text-sky-300">
          {m[4]}
        </code>,
      );
    } else if (m[5] !== undefined) {
      out.push(
        <a
          key={key}
          href={m[7]}
          target="_blank"
          rel="noreferrer"
          className="text-sky-400 underline hover:text-sky-300"
        >
          {m[6]}
        </a>,
      );
    } else if (m[8] !== undefined) {
      out.push(
        <em key={key} className="italic text-slate-300">
          {m[9]}
        </em>,
      );
    }
    lastIndex = INLINE_RE.lastIndex;
  }
  if (lastIndex < text.length) out.push(text.slice(lastIndex));
  return out;
}

function sectionColor(headingText: string): string {
  const t = headingText.toLowerCase();
  if (t === "added") return "text-emerald-400";
  if (t === "fixed") return "text-amber-400";
  if (t === "changed") return "text-sky-400";
  if (t === "removed" || t === "deprecated") return "text-red-400";
  return "text-slate-300";
}

function renderBlocks(blocks: Block[]): React.ReactNode {
  return blocks.map((b, i) => {
    if (b.type === "heading") {
      if (b.level === 1) return null; // redundant with the modal's own "Changelog" title
      if (b.level === 2) {
        return (
          <h3
            key={i}
            className="mt-6 border-t border-slate-800 pt-4 text-sm font-semibold text-slate-100 first:mt-0 first:border-t-0 first:pt-0"
          >
            {renderInline(b.text, `h-${i}`)}
          </h3>
        );
      }
      return (
        <h4 key={i} className={`mt-3 text-xs font-semibold uppercase tracking-wide ${sectionColor(b.text)}`}>
          {renderInline(b.text, `h-${i}`)}
        </h4>
      );
    }
    if (b.type === "paragraph") {
      return (
        <p key={i} className="mt-2 text-xs leading-relaxed text-slate-400">
          {renderInline(b.text, `p-${i}`)}
        </p>
      );
    }
    return (
      <ul key={i} className="mt-2 list-disc space-y-2 pl-4">
        {b.items.map((item, j) => (
          <li key={j} className="text-xs leading-relaxed text-slate-400">
            {item.parts.map((part, k) =>
              part.type === "p" ? (
                <p key={k} className={k > 0 ? "mt-1.5" : undefined}>
                  {renderInline(part.text, `li-${i}-${j}-${k}`)}
                </p>
              ) : (
                <ul key={k} className="mt-1.5 list-[circle] space-y-1 pl-4">
                  {part.items.map((sub, m) => (
                    <li key={m}>{renderInline(sub, `sub-${i}-${j}-${k}-${m}`)}</li>
                  ))}
                </ul>
              ),
            )}
          </li>
        ))}
      </ul>
    );
  });
}

export default function ChangelogModal({ onClose }: { onClose: () => void }) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getChangelog()
      .then((r) => {
        if (!cancelled) setContent(r.content);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const blocks = content ? parseMarkdown(content) : null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div
        className="flex max-h-[80vh] w-full max-w-2xl flex-col rounded-lg bg-slate-900 ring-1 ring-slate-700"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
          <h3 className="font-medium text-slate-100">Changelog</h3>
          <button onClick={onClose} className="rounded px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100">
            ✕
          </button>
        </div>
        <div className="overflow-y-auto px-4 py-3">
          {error && (
            <div className="rounded-md bg-red-500/10 px-3 py-2 text-xs text-red-400 ring-1 ring-red-500/30">
              Couldn't load the changelog: {error}
            </div>
          )}
          {!error && !blocks && <div className="py-6 text-center text-xs text-slate-500">Loading…</div>}
          {blocks && renderBlocks(blocks)}
        </div>
      </div>
    </div>
  );
}
