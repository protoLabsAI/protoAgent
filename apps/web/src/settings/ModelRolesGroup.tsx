import { StatusDot } from "@protolabsai/ui/data";
import { Popover } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { modelRoleStatusQuery, subagentsQuery, type ModelRoleStatus } from "../lib/queries";
import type { SettingsField } from "../lib/types";
import { ModelChainInput } from "./ModelChainInput";
import { settingListItems } from "./SettingsCategory";

type Overrides = Record<string, Record<string, unknown>>;

function StatusTip({ label, children, dot = false }: { label: string; children: ReactNode; dot?: boolean }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const ref = useRef<HTMLButtonElement>(null);
  return <Popover open={open} onOpenChange={(next) => { setOpen(next); if (!next) ref.current?.focus(); }}
    side="bottom" align="start" className="model-role-status-popover"
    trigger={<button ref={ref} type="button" className="pl-btn pl-btn--ghost pl-btn--sm model-role-status"
      aria-label={dot ? label : undefined} aria-expanded={open} aria-controls={id}>
      {dot ? <span aria-hidden><StatusDot status="warning" /></span> : label}
    </button>}>
    <div id={id}>{children}</div>
  </Popover>;
}

function recent(status: ModelRoleStatus | undefined, now: number) {
  const event = status?.last_fallback;
  const age = event ? now - Date.parse(event.time) : NaN;
  return age >= 0 && age < 86400000 ? event : null;
}

function fallbackStatus(status: ModelRoleStatus | undefined, statuses: ModelRoleStatus[], now: number) {
  if (status?.status === "unavailable" && status.reason === "no-vision") {
    return { dot: <span aria-hidden><StatusDot status="warning" /></span>, text: <StatusTip label="unavailable">
      None of these models can read images. Set its own chain with a vision model.
    </StatusTip> };
  }
  const event = recent(status, now);
  if (!event || !status) return {};
  const minutes = Math.floor((now - Date.parse(event.time)) / 60000);
  const age = minutes >= 60 ? `${Math.floor(minutes / 60)}h` : `${minutes}m`;
  const source = statuses.find((s) => s.role === status.source);
  const parentEvent = recent(source, now);
  const same = status.inherited && parentEvent && JSON.stringify(source?.chain) === JSON.stringify(status.chain)
    && JSON.stringify(parentEvent.from) === JSON.stringify(event.from) && parentEvent.reason === event.reason
    && parentEvent.to === event.to;
  const failures = event.from.map((model) => {
    const reason = event.reason.split("; ").find((part) => part.startsWith(`${model}: `))?.slice(model.length + 2) ?? event.reason;
    const explanation = /quota|usage_limit/i.test(reason) ? "was out of quota"
      : /429|RateLimit/i.test(reason) ? "was rate-limited"
      : /Timeout/i.test(reason) ? "timed out"
      : /5\d\d|Connection|Network/i.test(reason) ? "was down" : reason;
    return `${model} ${explanation}`;
  }).join("; ");
  const details = <>{same ? <p>Same fallback as {status.source}.</p> : null}
    <p>{event.to} answered {age} ago; {failures}.</p></>;
  return same
    ? { dot: <StatusTip dot label={`${status.role} fell back ${age} ago, the same as ${status.source}. Details`}>{details}</StatusTip> }
    : { dot: <span aria-hidden><StatusDot status="warning" /></span>, text: <StatusTip label={`fell back ${age} ago`}>{details}</StatusTip> };
}

export function ModelRolesGroup({ fields, valueOf, renderRow, overrides, onOverridesChange, canEditSubagents, auxChain }: {
  fields: SettingsField[];
  valueOf: (field: SettingsField) => unknown;
  renderRow: (field: SettingsField, dot?: ReactNode, status?: ReactNode, inheritanceDetail?: string) => ReactNode;
  overrides: Overrides;
  onOverridesChange: (value: Overrides) => void;
  canEditSubagents: boolean;
  auxChain: string[];
}) {
  const registry = useQuery({ ...subagentsQuery(), refetchOnMount: "always" });
  const history = useQuery(modelRoleStatusQuery());
  const [expanded, setExpanded] = useState(false);
  const [, setNow] = useState(Date.now);
  const now = Date.now();
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 30000); return () => clearInterval(timer); }, []);
  const listId = useId();
  const agents = registry.data?.subagents ?? [];
  // A subagent's chain comes from its unsaved edit (`overrides`) when there is one, else from
  // the registry's reported pin. `researcher`'s pin lives on a typed config field and never
  // lands in `subagent_overrides`, so without the registry fallback a saved researcher chain
  // would read as unpinned after the schema refetch (ADR 0133 M7c).
  const ownChain = (name: string) =>
    settingListItems(overrides[name]?.model ?? agents.find((agent) => agent.name === name)?.model).filter((id) => id.trim());
  const pinned = agents.filter((agent) => ownChain(agent.name).length).length;
  const count = agents.length;
  const summary = count === 1
    ? `1 subagent (${agents[0].name}) · ${pinned ? "has its own chain" : "follows the chat's model, else aux"}`
    : `${count} subagents${pinned ? ` · ${pinned} ${pinned === 1 ? "has its" : "have their"} own chain` : ""} · ${count - pinned} follow the chat's model, else aux`;
  const statuses = history.isError ? [] : history.data?.roles ?? [];
  const harvest = fields.find((field) => field.role === "harvest");
  return <div className="model-roles">
    {history.isError ? <p className="setting-desc">Fallback history isn't available right now. Chains still work.</p> : null}
    {["Main and aux", "Memory", "Checks", "Media"].map((subhead) => <section key={subhead}>
      <h3>{subhead}</h3>
      {fields.filter((field) => field.subhead === subhead).map((field) => {
        const status = fallbackStatus(statuses.find((s) => s.role === field.role), statuses, now);
        const detail = field.role === "facts" && harvest && !settingListItems(valueOf(harvest)).some((id) => id.trim())
          ? "Inherits harvest, which inherits aux's chain:" : undefined;
        return renderRow(field, status.dot, status.text, detail);
      })}
    </section>)}
    {registry.isError ? <p className="setting-desc">Subagent registry isn't available right now.</p> : null}
    {count ? <section><h3>Subagents</h3>
      <Button variant="ghost" size="sm" className="model-roles__registry" aria-expanded={expanded} aria-controls={listId}
        onClick={() => setExpanded(!expanded)}>{summary}</Button>
      <div id={listId} hidden={!expanded}>{expanded ? agents.map((agent) => {
        const field: SettingsField = { key: `subagents.${agent.name}.model`, label: agent.name, type: "model_chain",
          section: "Model roles", scope: "agent", source: "agent", options: fields.find((f) => f.type === "model_chain")?.options ?? [], restart: false, inherits: "aux" };
        return <div className="setting-row" key={agent.name}>
          <div className="setting-meta"><span className="setting-label">{agent.name}{!agent.enabled ? " (off)" : ""}</span></div>
          <div className="setting-control">{canEditSubagents ? <ModelChainInput field={field} value={ownChain(agent.name)}
            parentChain={auxChain} inheritedLabel="Follows the chat's /model pick, else aux"
            onChange={(model) => onOverridesChange({ ...overrides, [agent.name]: { ...overrides[agent.name], model } })} />
            : <p className="setting-desc">Subagent overrides are locked.</p>}</div>
        </div>;
      }) : null}</div>
    </section> : null}
  </div>;
}
