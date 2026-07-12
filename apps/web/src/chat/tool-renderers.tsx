import { AlertTriangle, ExternalLink, Hourglass, Image as ImageIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Badge } from "@protolabsai/ui/primitives";

import { parseMultimodalEnvelope, type MultimodalEnvelope } from "./multimodalEnvelope";
import { humanizeSeconds, parseWaitInput, summarizeThen, type WaitInfo } from "./waitInfo";

// Renders a tool's input/output as real components instead of a raw JSON blob.
//
// Two layers:
//   1. A generic structured renderer — objects become key/value field rows,
//      arrays become lists, URLs become links, scalars become chips, and plain
//      text wraps with inline link detection. This handles every tool *input*
//      (they're JSON objects) and any JSON output.
//   2. A small per-tool registry for our starter tools' *output* strings, whose
//      formats we own (calculator, web_search, fetch_url, current_time). Each
//      renderer is defensive: it returns null on an unexpected shape and the
//      generic text renderer takes over.

type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };

const URL_RE = /\bhttps?:\/\/[^\s)<>"']+/g;

function tryParseJson(raw: string): JsonValue | undefined {
  const t = raw.trim();
  if (!(t.startsWith("{") || t.startsWith("["))) return undefined;
  try {
    return JSON.parse(t) as JsonValue;
  } catch {
    return undefined;
  }
}

const isUrl = (s: string) => /^https?:\/\/\S+$/.test(s.trim());

/** Render a tool input or output as components. */
export function ToolValue({
  raw,
  role,
  tool,
  input,
}: {
  raw: string;
  role: "input" | "output";
  tool: string;
  /** The call's input-args preview, for output renderers that need BOTH sides (the `wait`
   *  waiting state derives duration/resume-plan from the args, #1914). Optional so the
   *  input-role call sites and older callers are untouched. */
  input?: string;
}) {
  const text = raw ?? "";

  // Multimodal envelope (#1947): a sentinel-prefixed JSON whose images[] carry base64.
  // Checked FIRST for outputs — the choke point — so no error/per-tool/JSON renderer ever
  // sees the sentinel; the expander shows the text part + an image-count note, never the
  // raw envelope.
  if (role === "output") {
    const mm = parseMultimodalEnvelope(text);
    if (mm) return <MultimodalBlock env={mm} />;
  }
  // Tool errors render uniformly regardless of which tool produced them. (This also keeps a
  // FAILED `wait` — "Error: couldn't schedule the wake-up" — on the error path, not the
  // waiting block below.)
  if (role === "output" && /^error\b/i.test(text.trim())) {
    return <ErrorBlock text={text} />;
  }
  // `wait` (#1914): the agent yielded ON PURPOSE and scheduled its own resume — say so,
  // instead of a generic success string the user can't act on. Derived from the INPUT args
  // (`{seconds, then}`), the structured side of the call; the output string stays untouched
  // server-side. An unparseable args preview (800-char truncation, mid-stream) falls through
  // to the plain render.
  if (role === "output" && tool === "wait") {
    const info = parseWaitInput(input);
    if (info) return <WaitBlock info={info} />;
  }
  // Tool-specific output renderers (known starter-tool formats).
  if (role === "output") {
    const custom = OUTPUT_RENDERERS[tool]?.(text);
    if (custom) return <>{custom}</>;
  }
  // Generic structured rendering.
  const parsed = tryParseJson(text);
  if (parsed !== undefined && typeof parsed === "object" && parsed !== null) {
    return Array.isArray(parsed) ? <ValueList items={parsed} /> : <KeyValueGrid obj={parsed} />;
  }
  return <TextBlock text={text} />;
}

// ── Generic structured primitives ───────────────────────────────────────────

function KeyValueGrid({ obj }: { obj: { [k: string]: JsonValue } }) {
  const entries = Object.entries(obj);
  if (!entries.length) return <TextBlock text="(empty)" />;
  return (
    <dl className="tool-kv">
      {entries.map(([k, v]) => (
        <div className="tool-kv-row" key={k}>
          <dt className="tool-kv-key">{k}</dt>
          <dd className="tool-kv-val">
            <ValueCell value={v} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function ValueList({ items }: { items: JsonValue[] }) {
  if (!items.length) return <TextBlock text="(empty list)" />;
  return (
    <ul className="tool-vlist">
      {items.map((v, i) => (
        <li key={i}>
          <ValueCell value={v} />
        </li>
      ))}
    </ul>
  );
}

function ValueCell({ value }: { value: JsonValue }): ReactNode {
  if (value === null) return <span className="tool-null">null</span>;
  if (typeof value === "boolean" || typeof value === "number") {
    return <span className="tool-chip">{String(value)}</span>;
  }
  if (typeof value === "string") {
    return isUrl(value) ? <Link href={value} /> : <span className="tool-scalar">{value}</span>;
  }
  if (Array.isArray(value)) return <ValueList items={value} />;
  return <KeyValueGrid obj={value} />;
}

function Link({ href, label }: { href: string; label?: string }) {
  return (
    <a className="tool-link" href={href} target="_blank" rel="noreferrer noopener">
      {label ?? href}
      <ExternalLink size={11} />
    </a>
  );
}

function linkify(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  URL_RE.lastIndex = 0;
  while ((m = URL_RE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(<Link key={key++} href={m[0]} />);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function TextBlock({ text }: { text: string }) {
  return <div className="tool-text">{linkify(text)}</div>;
}

/** Multimodal tool result (#1947): the envelope's text + an image-count note. When the
 *  server's 800-char preview cut the envelope, the count is unknowable (null) and — if even
 *  the text was cut — the note alone stands in as a generic label. */
function MultimodalBlock({ env }: { env: MultimodalEnvelope }) {
  const note =
    env.imageCount === null
      ? env.text
        ? "images attached (preview truncated)"
        : "multimodal tool result (preview truncated)"
      : env.imageCount > 0
        ? `${env.imageCount} image${env.imageCount === 1 ? "" : "s"} attached`
        : null;
  return (
    <div className="tool-multimodal">
      {env.text ? <TextBlock text={env.text} /> : null}
      {note ? (
        <span className="tool-multimodal-note">
          <ImageIcon size={12} />
          {note}
        </span>
      ) : null}
    </div>
  );
}

/** Waiting state for a successful `wait` (#1914): the agent ended its turn deliberately and
 *  will resume itself. Distinct hourglass presentation (not the generic success check), the
 *  humanized duration, a one-line summary of the resume plan, and the reassurance that the
 *  chat stays usable. Static by design — no ticking countdown (that would need new state
 *  plumbing for a card that is usually collapsed anyway). */
function WaitBlock({ info }: { info: WaitInfo }) {
  return (
    <div className="tool-wait">
      <div className="tool-wait-head">
        <Hourglass size={13} />
        <span>
          Waiting ~{humanizeSeconds(info.seconds)} — the agent yielded and will resume itself
        </span>
      </div>
      {info.then ? (
        <div className="tool-wait-then">
          Resumes with: <span className="tool-wait-plan">{summarizeThen(info.then)}</span>
        </div>
      ) : null}
      <div className="tool-wait-hint">
        You can keep chatting — the agent picks this up automatically.
      </div>
    </div>
  );
}

function ErrorBlock({ text }: { text: string }) {
  return (
    <div className="tool-error">
      <AlertTriangle size={13} />
      <span>{text.replace(/^error:\s*/i, "")}</span>
    </div>
  );
}

// ── Per-tool output renderers ────────────────────────────────────────────────

const OUTPUT_RENDERERS: Record<string, (raw: string) => ReactNode | null> = {
  calculator: renderCalculator,
  web_search: renderWebSearch,
  fetch_url: renderFetchUrl,
  current_time: renderCurrentTime,
};

function renderCalculator(raw: string): ReactNode | null {
  const m = raw.trim().match(/^([^\n]+?)\s*=\s*([^\n]+)$/);
  if (!m) return null;
  return (
    <div className="tool-calc">
      <code>{m[1]}</code>
      <span className="tool-calc-eq">=</span>
      <strong>{m[2]}</strong>
    </div>
  );
}

function renderCurrentTime(raw: string): ReactNode | null {
  const lines = raw.split("\n");
  const human = lines[1]?.startsWith("Human:") ? lines[1].slice("Human:".length).trim() : null;
  if (!human) return null;
  return (
    <div className="tool-time">
      <span className="tool-mono">{lines[0]}</span>
      <span className="tool-time-human">{human}</span>
    </div>
  );
}

function renderFetchUrl(raw: string): ReactNode | null {
  const m = raw.match(/^\[(\d+)\]\s+(\S+)\n\n([\s\S]*)$/);
  if (!m) return null;
  const [, status, url, body] = m;
  return (
    <div className="tool-fetch">
      <div className="tool-fetch-head">
        <Badge status="success">{status}</Badge>
        <Link href={url} />
      </div>
      <div className="tool-text tool-fetch-body">{body}</div>
    </div>
  );
}

type SearchResult = { title: string; url: string; snippet?: string };

function renderWebSearch(raw: string): ReactNode | null {
  const lines = raw.split("\n");
  if (!/^\d+ result\(s\) for /.test(lines[0] || "")) return null;
  const results: SearchResult[] = [];
  for (let i = 1; i < lines.length; i++) {
    const head = lines[i].match(/^\d+\.\s+(.*?)\s+—\s+(\S*)$/);
    if (head) {
      const next = lines[i + 1];
      const snippet = next && next.startsWith("   ") ? next.trim() : undefined;
      if (snippet) i++;
      results.push({ title: head[1], url: head[2], snippet });
    }
  }
  if (!results.length) return null;
  return (
    <ol className="tool-results">
      {results.map((r, i) => (
        <li className="tool-result" key={i}>
          {r.url ? <Link href={r.url} label={r.title} /> : <span className="tool-scalar">{r.title}</span>}
          {r.snippet ? <span className="tool-result-snippet">{r.snippet}</span> : null}
        </li>
      ))}
    </ol>
  );
}
