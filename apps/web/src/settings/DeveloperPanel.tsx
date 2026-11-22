import { Switch } from "@protolabsai/ui/forms";
import { useToast } from "@protolabsai/ui/overlays";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";

import {
  resetAllFlags,
  resetFlag,
  saveFlag,
  useFlag,
  useFlagOverrides,
  useFlags,
} from "../flags/flags";
import type { FlagInfo, FlagTier } from "../lib/types";

// Settings → Developer (ADR 0068) — view + toggle pre-release feature flags for THIS
// agent. Only surfaced off prod (a dev build / a non-prod channel / ?dev), so production
// users never see it. A toggle is stored on the agent (PUT /api/flags/<id>), so a flag
// that gates server behaviour really turns on and stays on across restarts; an agent too
// old to store one keeps the toggle on this device. Channel + tiers come from /api/flags.

const TIER_STATUS: Record<FlagTier, "neutral" | "warning" | "info" | "success"> = {
  off: "neutral",
  dev: "warning",
  beta: "info",
  on: "success",
};

function FlagRow({ flag }: { flag: FlagInfo }) {
  const toast = useToast();
  const effective = useFlag(flag.id);
  const override = useFlagOverrides((s) => s.overrides[flag.id]);
  const overridden = override !== undefined || flag.source === "override";
  // Pinned by PROTOAGENT_FLAG_<ID>: the agent would refuse a change, so don't offer one.
  const pinned = flag.source === "env";
  const failed = (err: unknown) =>
    toast({ tone: "error", title: "Couldn't change the flag", message: err instanceof Error ? err.message : String(err) });
  const toggle = (on: boolean) => {
    saveFlag(flag.id, on).then((where) => {
      if (where === "device") {
        toast({ tone: "info", title: "Saved on this device only", message: "This agent is too old to store the flag, so its server-side behaviour won't change." });
      }
    }, failed);
  };
  return (
    <div className="setting-row" data-key={`flag.${flag.id}`}>
      <div className="setting-meta">
        <span className="setting-label">
          <code>{flag.id}</code> <Badge status={TIER_STATUS[flag.tier]}>{flag.tier}</Badge>
          {overridden ? <Badge status="info">overridden</Badge> : null}
          {pinned ? <Badge status="neutral">set by environment</Badge> : null}
        </span>
        <p className="setting-desc">
          {flag.description}
          {flag.owner ? <span className="muted"> · owner {flag.owner}</span> : null}
          {flag.remove_by ? <span className="muted"> · remove by {flag.remove_by}</span> : null}
        </p>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Switch
          id={`flag-${flag.id}`}
          checked={effective}
          disabled={pinned}
          onCheckedChange={toggle}
          label={effective ? "on" : "off"}
        />
        {overridden ? (
          <Button variant="ghost" size="sm" onClick={() => void resetFlag(flag.id).catch(failed)} title="Reset to the channel default">
            Reset
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export function DeveloperPanel() {
  const { data } = useFlags();
  const localCount = useFlagOverrides((s) => Object.keys(s.overrides).length);
  const flags = data?.flags ?? [];
  const overrideCount = localCount + flags.filter((f) => f.source === "override").length;
  const channel = data?.channel ?? "prod";
  return (
    <section className="panel stage-panel" data-testid="developer-panel">
      <PanelHeader
        title="Developer"
        kicker={`pre-release feature flags · channel ${channel} · toggles saved on this agent`}
        actions={
          <Button variant="ghost" size="sm" onClick={() => void resetAllFlags()} disabled={overrideCount === 0}>
            Reset all
          </Button>
        }
      />
      <div className="stage-body">
        {flags.length === 0 ? (
          <Empty>
            No developer flags are registered. Add one in <code>src/runtime/flags.py</code>.
          </Empty>
        ) : (
          flags.map((f) => <FlagRow key={f.id} flag={f} />)
        )}
      </div>
    </section>
  );
}
