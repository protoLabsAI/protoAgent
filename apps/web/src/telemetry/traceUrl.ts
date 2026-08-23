// Langfuse deep-link for a telemetry row (#trace_id pivot).
//
// A trace lives at `<host>/project/<project_id>/traces/<trace_id>` — the console
// knows neither the host nor the project id, so the server hands us a template
// (`langfuse_trace_url_template` on /api/telemetry/recent) with a `{trace_id}`
// placeholder. When Langfuse isn't configured there is no template, and the
// surface falls back to a copyable trace id rather than a broken link.

export function langfuseTraceUrl(
  template: string | null | undefined,
  traceId: string | null | undefined,
): string | null {
  const id = (traceId || "").trim();
  const tpl = (template || "").trim();
  if (!id || !tpl || !tpl.includes("{trace_id}")) return null;
  // Only ever emit an http(s) link — never let a malformed template turn into a
  // `javascript:` href.
  if (!/^https?:\/\//i.test(tpl)) return null;
  return tpl.replace("{trace_id}", encodeURIComponent(id));
}

// What one row's Trace cell should render (#3017).
//
// The distinction that matters is `off` vs `none`. Both are blank rows, but they
// mean opposite things: `none` is "this turn wasn't traced" (Langfuse is on, this
// row predates the column or its span never landed), while `off` is "tracing is
// disabled on this instance". Rendering `none` for both is how a fleet ran 336
// turns and 5,000 model calls with tracing dark and nothing in the product said so
// — the column just looked like a run of untraced turns.
export type TraceCellState =
  | { kind: "link"; href: string; short: string }
  | { kind: "copy"; traceId: string; short: string }
  | { kind: "off" }
  | { kind: "none" };

export function traceCellState(
  template: string | null | undefined,
  traceId: string | null | undefined,
  tracingEnabled: boolean,
): TraceCellState {
  const id = (traceId || "").trim();
  // A row that HAS a trace id always shows it, even if the instance has since had
  // tracing turned off — the trace still exists in Langfuse, and an honest id beats
  // hiding it behind a global flag.
  if (!id) return tracingEnabled ? { kind: "none" } : { kind: "off" };
  const short = id.slice(0, 8);
  const href = langfuseTraceUrl(template, id);
  return href ? { kind: "link", href, short } : { kind: "copy", traceId: id, short };
}
