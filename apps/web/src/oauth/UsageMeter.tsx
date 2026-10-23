import { Progress } from "@protolabsai/ui/data";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { onTopic } from "../lib/events";
import { usageQuery } from "../lib/queries";

function countdown(reset: number, now: number) {
  const minutes = Math.max(0, Math.ceil((reset - now) / 60_000));
  if (!minutes) return "now";
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  return `${days ? `${days}d ` : ""}${hours ? `${hours}h ` : ""}${minutes % 60}m`;
}

/** Mounted only by the Providers account card; query polling and topic subscription
 * stop with the view. The timer updates presentation only, never fetches. */
export function UsageMeter({ provider }: { provider: string }) {
  const qc = useQueryClient();
  const query = useQuery(usageQuery(provider));
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => onTopic("provider.usage", (data) => {
    if (data.provider_id === provider) {
      void qc.invalidateQueries({ queryKey: usageQuery(provider).queryKey });
    }
  }), [provider, qc]);

  const account = query.data?.accounts?.find((a) => a.provider_id === provider);
  const unavailable = query.isError || !account || account.status !== "ok" || account.stale
    || (account.stale_after && Date.parse(account.stale_after) <= now) || !account.windows?.length;
  return (
    <div data-testid="usage-meter" style={{ display: "grid", gap: "var(--pl-space-2)", marginTop: "var(--pl-space-2)" }}>
      {unavailable ? (
        <span style={{ color: "var(--pl-color-fg-muted)" }}>Usage unavailable</span>
      ) : account.windows!.map((window) => {
        const scope = window.scope?.model || window.scope?.bucket;
        const label = `${window.kind.charAt(0).toUpperCase()}${window.kind.slice(1)}${scope ? ` · ${scope}` : ""}`;
        const reset = Date.parse(window.resets_at);
        const valid = Number.isFinite(window.used_pct) && window.used_pct >= 0 && window.used_pct <= 100
          && Number.isFinite(reset) && ["ok", "warning", "blocked"].includes(window.status);
        if (!valid) return <span key={window.id} style={{ color: "var(--pl-color-fg-muted)" }}>{label} — Usage unavailable</span>;
        const state = window.status === "blocked" || window.used_pct >= 100 ? "blocked"
          : window.status === "warning" || window.used_pct >= 80 ? "warning" : "ok";
        return (
          <div key={window.id} role="group" aria-label={label} data-state={state}>
            <Progress value={window.used_pct} showValue label={`${label} · ${state === "ok" ? "Available" : state === "warning" ? "Warning" : "Blocked"}`}
              status={state === "blocked" ? "error" : state === "warning" ? "warning" : "success"} />
            <small style={{ color: "var(--pl-color-fg-muted)" }}>
              {state === "blocked" ? `Blocked until ${new Date(reset).toLocaleString()} · ` : ""}
              Resets {reset <= now ? "now" : `in ${countdown(reset, now)}`}
            </small>
          </div>
        );
      })}
    </div>
  );
}
