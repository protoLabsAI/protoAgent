import "../fleet/fleet.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link2, Pencil, Play, Plus, Radar, Square, Trash2 } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { Alert, StatusDot } from "@protolabsai/ui/data";
import { EditableText, Switch } from "@protolabsai/ui/forms";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { PanelHeader } from "@protolabsai/ui/navigation";

import { api, currentSlug } from "../lib/api";
import { errMsg } from "../lib/format";
import { fleetQuery, queryKeys } from "../lib/queries";
import type { DiscoveredAgent, FleetAgent } from "../lib/types";

// Fleet manager (ADR 0042) — Settings → Agents. Lists the workspace agents with live
// status (the query polls every 3s, so a crashed agent flips to stopped on its own) and
// per-row start / stop / remove. "+ New agent" opens the archetype picker via `onNew`.
export function FleetManagerPanel({ onNew }: { onNew?: () => void }) {
  const qc = useQueryClient();
  const fleet = useQuery(fleetQuery());
  const [busy, setBusy] = useState<string | null>(null); // name currently being acted on
  const [confirmRemove, setConfirmRemove] = useState<FleetAgent | null>(null);
  const [purge, setPurge] = useState(false);
  // Transient action feedback (rename / start / stop / add / discover failures) is a TOAST —
  // the global toaster, not an inline line. The in-progress state already rides each button's
  // disabled spinner, so there's no "…ing" toast. (The one exception is the actionable
  // enable-delegates banner below, which carries a retry button a toast can't.)
  const toast = useToast();

  // Display rename (the id — and so the URL slug + data scope — never changes).
  const [renaming, setRenaming] = useState<string | null>(null); // the id being renamed
  const rename = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => api.renameAgent(id, name),
    onError: (e) => toast({ tone: "error", title: "Rename failed", message: errMsg(e) }),
    onSettled: () => {
      setRenaming(null);
      qc.invalidateQueries({ queryKey: queryKeys.fleet });
    },
  });

  const agents = fleet.data?.agents ?? [];
  const slug = currentSlug(); // the agent this window is focused on (the URL slug)
  // Hub↔remote version handshake (ADR 0042 §I): the proxied /api/* surface has no
  // other versioning, so a remote member on a different release gets a warning badge.
  const hubVersion = agents.find((a) => a.host)?.version ?? "";

  const run = useMutation({
    mutationFn: async (fn: () => Promise<unknown>) => fn(),
    onError: (e) => toast({ tone: "error", title: "Action failed", message: errMsg(e) }),
    onSettled: () => {
      setBusy(null);
      qc.invalidateQueries({ queryKey: queryKeys.fleet });
    },
  });
  const act = (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    run.mutate(fn);
  };

  // Agent-to-agent flows (ADR 0042 + 0025): add another agent as a `delegate_to` target of the
  // FOCUSED agent (this window's slug), pointing at its A2A endpoint — then this agent can
  // delegate work to it. The delegates query/POST is slug-scoped, so it lands on the focused agent.
  const delegatesQ = useQuery({ queryKey: ["delegates"], queryFn: () => api.delegates(), retry: false });
  const delegateNames = new Set((delegatesQ.data?.delegates ?? []).map((d) => d.name));
  // When an add 404s (the focused agent doesn't serve /api/delegates), we keep the attempted
  // entry so "Enable delegates" can retry it after enabling the plugin. Fleet agents ship with
  // delegates enabled at create (ADR 0042); the HOST carries no plugins by default — enabling
  // appends to plugins.enabled via applyConfig, and the reload hot-mounts the routes (#797),
  // so the retry succeeds without a restart.
  const [needsEnable, setNeedsEnable] = useState<{ name: string; url: string } | null>(null);
  const addDelegate = useMutation({
    mutationFn: (entry: { name: string; url: string }) =>
      api.createDelegate({ name: entry.name, type: "a2a", url: entry.url }),
    onMutate: () => setNeedsEnable(null),
    onError: (e: Error, entry) => {
      // A 404 means the focused agent doesn't serve /api/delegates yet — surface the
      // actionable enable-and-retry banner (needsEnable) instead of a transient toast,
      // since it carries a retry button. Any other failure is a plain error toast.
      if (/404|not found/i.test(e.message)) {
        setNeedsEnable(entry);
      } else {
        toast({ tone: "error", title: "Couldn't add delegate", message: errMsg(e) });
      }
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["delegates"] });
      void delegatesQ.refetch();
    },
  });
  const enableDelegates = useMutation({
    mutationFn: async (entry: { name: string; url: string }) => {
      const { config } = await api.config(); // the FOCUSED agent's live config (slug-scoped)
      const enabled = config.plugins?.enabled ?? [];
      if (!enabled.includes("delegates")) {
        await api.applyConfig({ plugins: { enabled: [...enabled, "delegates"] } }, null);
      }
      return entry;
    },
    onSuccess: (entry) => {
      setNeedsEnable(null);
      addDelegate.mutate(entry); // routes are hot-mounted on the reload — retry the add now
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't enable delegates", message: errMsg(e) }),
  });

  // Network discovery (ADR 0042 §I) — scan the box + LAN for OTHER protoAgents (not in this
  // fleet), then add a found one as a delegate of the focused agent (its A2A = url + /a2a).
  const [scanning, setScanning] = useState(false);
  const [discovered, setDiscovered] = useState<DiscoveredAgent[] | null>(null);
  const scan = async () => {
    setScanning(true);
    try {
      setDiscovered((await api.discoverAgents()).discovered);
    } catch (e) {
      toast({ tone: "error", title: "Discovery failed", message: errMsg(e) });
    } finally {
      setScanning(false);
    }
  };
  // Remote adds funnel through the same mutation, so a host-window 404 gets the
  // same enable-and-retry path as fleet-row adds.
  const addRemote = (d: DiscoveredAgent) => addDelegate.mutate({ name: d.name, url: `${d.url}/a2a` });

  // …or join the fleet outright (ADR 0042 §I): the remote becomes a SWITCHABLE member —
  // a slug window, console + A2A reverse-proxied through this hub. Discovered names can
  // collide with existing agents (every template fork is "protoagent") — suffix on 400.
  const addMember = useMutation({
    mutationFn: (d: DiscoveredAgent) => api.addRemoteAgent({ name: d.name, url: d.url }),
    onError: (e) => toast({ tone: "error", title: "Couldn't add to fleet", message: errMsg(e) }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queryKeys.fleet });
      void scan(); // the new member drops out of the discover list
    },
  });
  const removeMember = useMutation({
    mutationFn: (a: FleetAgent) => api.removeRemoteAgent(a.id),
    onError: (e) => toast({ tone: "error", title: "Couldn't remove member", message: errMsg(e) }),
    onSettled: () => qc.invalidateQueries({ queryKey: queryKeys.fleet }),
  });

  return (
    <section className="panel stage-panel">
      <PanelHeader
        title="Agents"
        kicker={`${agents.length} agent${agents.length === 1 ? "" : "s"} on this host · the fleet`}
        actions={
          <Button variant="primary" onClick={onNew}>
            <Plus size={15} /> New agent
          </Button>
        }
      />
      <div className="stage-body">
        {/* The one error that stays inline: a 404 add-delegate carries an actionable
            enable-and-retry banner (the focused agent's delegates plugin isn't enabled).
            Plain action failures are transient toasts; this one needs its retry button. */}
        {needsEnable ? (
          <Alert
            status="error"
            action={
              <Button
                variant="default"
                disabled={enableDelegates.isPending || addDelegate.isPending}
                onClick={() => enableDelegates.mutate(needsEnable)}
                data-testid="enable-delegates">
                {enableDelegates.isPending ? "Enabling…" : "Enable delegates on this agent"}
              </Button>
            }
          >
            This agent can't hold delegates yet — the delegates plugin isn't enabled on it.
          </Alert>
        ) : null}
        {fleet.isLoading ? (
          <Empty>Loading the fleet…</Empty>
        ) : agents.length === 0 ? (
          <Empty
            title="No agents yet"
            description="create one to get started"
            action={
              <Button variant="primary" onClick={onNew}>
                <Plus size={15} /> New agent
              </Button>
            }
          />
        ) : (
          <ul className="fleet-list">
            {agents.map((a) => {
              const isActive = (a.host ? "host" : a.id) === slug; // slug = stable id, not name
              return (
                <li key={a.id} className={`fleet-row${isActive ? " active" : ""}`}>
                  <span role="img" title={a.running ? "running" : "stopped"} aria-label={a.running ? "running" : "stopped"}>
                    <StatusDot status={a.running ? "success" : "neutral"} pulse={a.running} />
                  </span>
                  <div className="fleet-row-main">
                    <span className="fleet-name">
                      {renaming === a.id ? (
                        <EditableText
                          inputClassName="fleet-rename-input"
                          value={a.name}
                          editing
                          commitOnBlur={false}
                          aria-label="New agent name"
                          validate={(v) => v.length > 0}
                          onEditingChange={(e) => setRenaming(e ? a.id : null)}
                          onCommit={(next) => rename.mutate({ id: a.id, name: next })}
                        />
                      ) : (
                        a.name
                      )}
                      {a.host ? <Badge status="neutral">this instance</Badge> : null}
                      {a.remote ? (
                        <span title="A remote fleet member — proxied by URL">
                          <Badge status="neutral">remote</Badge>
                        </span>
                      ) : null}
                      {/* Version skew — remotes (hub↔remote handshake) AND local members:
                          a local member spawned before an app update keeps running the OLD
                          binary until restarted (version-coherence P2), so it gets the same
                          warning badge a drifted remote does. */}
                      {a.version && hubVersion && a.version !== hubVersion ? (
                        <span
                          data-testid="fleet-version-skew"
                          title={`This ${a.remote ? "remote" : "member"} runs v${a.version}, the hub runs v${hubVersion} — ${a.remote ? "features may misbehave across the version gap." : "restart it (Stop → Start) to pick up the hub's binary."}`}
                        >
                          <Badge status="warning">v{a.version}</Badge>
                        </span>
                      ) : null}
                      {isActive ? <Badge status="info">active</Badge> : null}
                    </span>
                    <span className="fleet-meta">
                      {a.remote ? a.url : `:${a.port}`}
                      {a.pid ? ` · pid ${a.pid}` : ""}
                      {a.bundle ? ` · ${a.bundle}` : ""}
                    </span>
                  </div>
                  <div className="fleet-row-actions">
                    {/* Add as a delegate of the focused agent → enables delegate_to flows. Any
                        agent but the one you're on (it can't delegate to itself). */}
                    {!isActive ? (
                      delegateNames.has(a.name) ? (
                        <span title="A delegate of this agent">
                          <Badge status="info">delegate</Badge>
                        </span>
                      ) : (
                        <Button icon variant="ghost" title="Add as a delegate of this agent (delegate_to)"
                          disabled={addDelegate.isPending || !a.a2a}
                          onClick={() => addDelegate.mutate({ name: a.name, url: a.a2a! })}>
                          <Link2 size={14} />
                        </Button>
                      )
                    ) : null}
                    {/* A remote member can't be started/stopped/renamed from here — only
                        unregistered (the remote agent itself is untouched). */}
                    {a.remote ? (
                      <Button icon variant="ghost" title="Remove from this fleet (the remote agent is untouched)"
                        disabled={removeMember.isPending}
                        onClick={() => removeMember.mutate(a)}>
                        <Trash2 size={14} />
                      </Button>
                    ) : null}
                    {/* The host serves this console — it can't stop or remove itself; its
                        display name is edited in Settings → Identity instead. */}
                    {a.host || a.remote ? null : (
                      <>
                        <Button icon variant="ghost" title="Rename (display name only — the id/URL stays)"
                          disabled={rename.isPending}
                          onClick={() => setRenaming(a.id)}>
                          <Pencil size={14} />
                        </Button>
                        {a.running ? (
                          <Button icon variant="ghost" title="Stop" disabled={busy === a.name}
                            onClick={() => act(a.name, () => api.stopAgent(a.name))}>
                            <Square size={14} />
                          </Button>
                        ) : (
                          <Button icon variant="ghost" title="Start" disabled={busy === a.name}
                            onClick={() => act(a.name, () => api.startAgent(a.name))}>
                            <Play size={14} />
                          </Button>
                        )}
                        <Button icon variant="ghost" title="Remove" disabled={busy === a.name}
                          onClick={() => { setPurge(false); setConfirmRemove(a); }}>
                          <Trash2 size={14} />
                        </Button>
                      </>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {/* Network discovery — scan the box + LAN for OTHER protoAgents and add one as a remote
            delegate of the focused agent (ADR 0042 §I). */}
        <div className="fleet-discover">
          <Button variant="ghost" onClick={scan} disabled={scanning}>
            <Radar size={14} /> {scanning ? "Scanning…" : "Discover agents on the network"}
          </Button>
          {discovered ? (
            discovered.length === 0 ? (
              <Empty>No other protoAgents found on the network.</Empty>
            ) : (
              <ul className="fleet-list">
                {discovered.map((d) => (
                  <li key={d.url} className="fleet-row">
                    <StatusDot status="success" />
                    <div className="fleet-row-main">
                      <span className="fleet-name">{d.name}</span>
                      <span className="fleet-meta">{d.url}</span>
                    </div>
                    <div className="fleet-row-actions">
                      {/* Two ways in: a delegate of the FOCUSED agent (delegate_to flows), or a
                          full fleet MEMBER — a switchable slug window proxied through this hub. */}
                      <Button icon variant="ghost" title="Add to this fleet (a switchable remote member)"
                        disabled={addMember.isPending}
                        onClick={() => addMember.mutate(d)}>
                        <Plus size={14} />
                      </Button>
                      {delegateNames.has(d.name) ? (
                        <span title="A delegate of this agent">
                          <Badge status="info">delegate</Badge>
                        </span>
                      ) : (
                        <Button icon variant="ghost" title="Add as a remote delegate (delegate_to)"
                          disabled={addDelegate.isPending}
                          onClick={() => addRemote(d)}>
                          <Link2 size={14} />
                        </Button>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )
          ) : null}
        </div>
      </div>

      <ConfirmDialog
        open={confirmRemove !== null}
        title={confirmRemove ? `Remove ${confirmRemove.name}?` : ""}
        confirmLabel={purge ? "Remove + purge data" : "Remove"}
        destructive
        onConfirm={() => {
          const a = confirmRemove;
          const wipe = purge;
          setConfirmRemove(null);
          if (a) act(a.name, () => api.removeAgent(a.name, wipe));
        }}
        onClose={() => setConfirmRemove(null)}
      >
        <p>Stops the agent and removes it from the fleet.</p>
        <Switch
          className="fleet-purge"
          checked={purge}
          onCheckedChange={setPurge}
          label="Also purge its workspace data (irreversible)"
        />
      </ConfirmDialog>
    </section>
  );
}
