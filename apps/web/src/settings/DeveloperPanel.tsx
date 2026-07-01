import { Switch } from "@protolabsai/ui/forms";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";

import {
  clearFlagOverride,
  resetFlagOverrides,
  setFlagOverride,
  useFlag,
  useFlagOverrides,
  useFlags,
} from "../flags/flags";
import type { FlagInfo, FlagTier } from "../lib/types";

// Settings → Developer (ADR 0068) — view + toggle pre-release feature flags for THIS
// device/session. Only surfaced off prod (a dev build / a non-prod channel / ?dev), so
// production users never see it. Toggles are device-local (they never touch shared config);
// the channel + tiers come from the server (/api/flags).

const TIER_STATUS: Record<FlagTier, "neutral" | "warning" | "info" | "success"> = {
  off: "neutral",
  dev: "warning",
  beta: "info",
  on: "success",
};

function FlagRow({ flag }: { flag: FlagInfo }) {
  const effective = useFlag(flag.id);
  const override = useFlagOverrides((s) => s.overrides[flag.id]);
  const overridden = override !== undefined;
  return (
    <div className="setting-row" data-key={`flag.${flag.id}`}>
      <div className="setting-meta">
        <span className="setting-label">
          <code>{flag.id}</code> <Badge status={TIER_STATUS[flag.tier]}>{flag.tier}</Badge>
          {overridden ? <Badge status="info">overridden</Badge> : null}
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
          onCheckedChange={(on) => setFlagOverride(flag.id, on)}
          label={effective ? "on" : "off"}
        />
        {overridden ? (
          <Button variant="ghost" size="sm" onClick={() => clearFlagOverride(flag.id)} title="Reset to the channel default">
            Reset
          </Button>
        ) : null}
      </div>
    </div>
  );
}

export function DeveloperPanel() {
  const { data } = useFlags();
  const overrideCount = useFlagOverrides((s) => Object.keys(s.overrides).length);
  const flags = data?.flags ?? [];
  const channel = data?.channel ?? "prod";
  return (
    <section className="panel stage-panel" data-testid="developer-panel">
      <PanelHeader
        title="Developer"
        kicker={`pre-release feature flags · channel ${channel} · toggles saved on this device`}
        actions={
          <Button variant="ghost" size="sm" onClick={resetFlagOverrides} disabled={overrideCount === 0}>
            Reset all
          </Button>
        }
      />
      <div className="stage-body">
        {flags.length === 0 ? (
          <Empty>
            No developer flags are registered. Add one in <code>runtime/flags.py</code>.
          </Empty>
        ) : (
          flags.map((f) => <FlagRow key={f.id} flag={f} />)
        )}
      </div>
    </section>
  );
}
