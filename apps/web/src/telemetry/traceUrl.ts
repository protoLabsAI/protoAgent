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
