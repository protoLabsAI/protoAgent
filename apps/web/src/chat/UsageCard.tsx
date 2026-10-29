import { Progress } from "@protolabsai/ui/data";
import { Card } from "@protolabsai/ui/primitives";

import { registerChatComponent } from "../ext/componentRegistry";
import { ACCOUNT_LABEL, clockTime, resetsIn, windowLabel, windowState, type UsageSnapshot } from "./usageView";

const muted = { color: "var(--pl-color-fg-muted)" };
const stack = { display: "grid", gap: "var(--pl-space-2)" };

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Validate persisted component props before passing them to labels or DS controls. */
function validSnapshot(value: unknown): value is UsageSnapshot {
  return record(value) && Array.isArray(value.accounts) && value.accounts.every((a) =>
    record(a) && typeof a.provider_id === "string" && typeof a.status === "string"
    && (a.windows === undefined || (Array.isArray(a.windows) && a.windows.every((w) =>
      record(w) && typeof w.id === "string" && typeof w.kind === "string"
      && typeof w.used_pct === "number" && Number.isFinite(w.used_pct) && w.used_pct >= 0 && w.used_pct <= 100
      && typeof w.resets_at === "string" && Number.isFinite(Date.parse(w.resets_at))
      && typeof w.status === "string" && ["ok", "warning", "blocked"].includes(w.status)
      && (w.scope == null || (record(w.scope)
        && (w.scope.model === undefined || typeof w.scope.model === "string")
        && (w.scope.bucket === undefined || typeof w.scope.bucket === "string")))))));
}

export function UsageCard({ snapshot, checked_at }: { snapshot: UsageSnapshot; checked_at: number }) {
  if (!validSnapshot(snapshot) || typeof checked_at !== "number" || !Number.isFinite(new Date(checked_at).getTime())) {
    return <Card><span style={muted}>Usage unavailable</span></Card>;
  }
  return (
    <Card role="group" aria-label="Subscription usage">
      <div style={stack}>
        <header style={{ display: "flex", justifyContent: "space-between", gap: "var(--pl-space-2)", flexWrap: "wrap" }}>
          <strong>Subscription usage</strong>
          <small style={muted} title={new Date(checked_at).toLocaleString()}>Checked {clockTime(checked_at)}</small>
        </header>
        {!snapshot.accounts.length ? <span style={muted}>No Claude or ChatGPT / Codex subscription is connected.</span> : null}
        {snapshot.accounts.map((account, index) => {
          const fetched = typeof account.fetched_at === "string" ? Date.parse(account.fetched_at) : NaN;
          const age = Math.floor((checked_at - fetched) / 60_000);
          return (
            <section key={index} style={stack}>
              <div>
                <strong>{typeof ACCOUNT_LABEL[account.provider_id] === "string" ? ACCOUNT_LABEL[account.provider_id] : account.provider_id}</strong>
                {age >= 1 ? <small style={muted}> · data from {age}m ago</small> : null}
                {account.stale ? <small style={muted}> · last known, refresh failed</small> : null}
              </div>
              {account.status !== "ok" || !account.windows?.length ? <span style={muted}>Usage unavailable</span> : account.windows.map((window, windowIndex) => {
                const label = windowLabel(window);
                const state = windowState(window);
                const reset = Date.parse(window.resets_at);
                return (
                  <div key={windowIndex} role="group" aria-label={label} data-state={state}>
                    <Progress value={window.used_pct} label={`${label} · ${Math.round(window.used_pct)}% used${state === "blocked" ? " · Limit reached" : state === "warning" ? " · Near limit" : ""}`}
                      status={state === "blocked" ? "error" : state === "warning" ? "warning" : "success"} />
                    <small style={muted}>Resets {reset <= checked_at ? "now" : `in ${resetsIn(reset, checked_at)}`}</small>
                  </div>
                );
              })}
            </section>
          );
        })}
      </div>
    </Card>
  );
}

registerChatComponent("subscription-usage", ({ props }) => (
  <UsageCard snapshot={props.snapshot as UsageSnapshot} checked_at={props.checked_at as number} />
));
