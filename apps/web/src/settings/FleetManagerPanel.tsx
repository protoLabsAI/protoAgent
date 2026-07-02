import "../fleet/fleet.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link2, Pencil, Play, Plus, Radar, Server, Square, Trash2 } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { Alert, StatusDot } from "@protolabsai/ui/data";
import { EditableText, Input, SecretInput, Switch } from "@protolabsai/ui/forms";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { PanelHeader } from "@protolabsai/ui/navigation";

import { QuickSetting } from "./QuickSetting";
import { api, currentSlug } from "../lib/api";
import { errMsg } from "../lib/format";
import { fleetQuery, queryKeys } from "../lib/queries";
import type { DiscoveredAgent, FleetAgent } from "../lib/types";

/** The manual add-remote form is submittable only with a name and an http(s) URL. Exported
 * (pure) so the enable rule is unit-tested without rendering the panel. The server does the
 * authoritative validation (charset, SSRF egress, dedupe); this is just the button gate. */
export function canAddRemote(name: string, url: string): boolean {
  return name.trim().length > 0 && /^https?:\/\/.+/.test(url.trim());
}

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
  // goes through the dedicated /api/plugins/{id}/enabled endpoint, and the reload hot-mounts the
  // routes (#797), so the retry succeeds without a restart.
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
        // Use the dedicated endpoint, not a raw plugins.enabled patch: it reconciles the
        // enabled/disabled lists, refuses builtins, and runs the install/surface logic a
        // hand-written `applyConfig({plugins:{enabled:[…]}})` would skip.
        await api.setPluginEnabled("delegates", true);
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

  // A registered member's up-front feedback: the server probes it at register time and
  // returns `reachable`, so a peer that's offline (or behind a wrong/missing token — the
  // probe hits its unauthenticated agent-card) is added with an honest "not reachable yet"
  // warning instead of silently appearing as a dead row.
  const addedToast = (name: string, reachable?: boolean) =>
    reachable === false
      ? toast({
          tone: "warning",
          title: `Added ${name}`,
          message: "Registered, but it isn't reachable yet — it'll connect when it comes online.",
        })
      : toast({ tone: "success", title: `Added ${name}`, message: `${name} joined the fleet.` });

  // …or join the fleet outright (ADR 0042 §I): the remote becomes a SWITCHABLE member —
  // a slug window, console + A2A reverse-proxied through this hub.
  const addMember = useMutation({
    mutationFn: (d: DiscoveredAgent) => api.addRemoteAgent({ name: d.name, url: d.url }),
    onSuccess: (res, d) => addedToast(d.name, res.reachable),
    onError: (e) => toast({ tone: "error", title: "Couldn't add to fleet", message: errMsg(e) }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queryKeys.fleet });
      void scan(); // the new member drops out of the discover list
    },
  });

  // The add-a-remote-by-URL form doubles as the EDIT form (`editingId` set). Manual add is the
  // ONLY way to register a token-gated remote (discovery can't carry a credential) or a peer
  // discovery didn't surface (a different subnet, mDNS off); edit is how you fix a rotated/
  // wrong token or a changed URL in place (the id/slug — and open windows — survive).
  const [showAdd, setShowAdd] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null); // null = add mode
  const [addName, setAddName] = useState("");
  const [addUrl, setAddUrl] = useState("");
  const [addToken, setAddToken] = useState("");
  const resetForm = () => {
    setShowAdd(false);
    setEditingId(null);
    setAddName("");
    setAddUrl("");
    setAddToken("");
  };
  const openAdd = () => {
    resetForm();
    setShowAdd(true);
  };
  const openEdit = (a: FleetAgent) => {
    setEditingId(a.id);
    setAddName(a.name);
    setAddUrl(a.url ?? "");
    setAddToken(""); // blank = keep the stored token (write a new one only to rotate it)
    setShowAdd(true);
  };
  const submitRemote = useMutation({
    mutationFn: () => {
      const name = addName.trim();
      const url = addUrl.trim();
      const token = addToken.trim();
      // On edit, a blank token means "keep" (omit it); on add, pass it through.
      return editingId
        ? api.updateRemoteAgent(editingId, { name, url, ...(token ? { token } : {}) })
        : api.addRemoteAgent({ name, url, token });
    },
    onSuccess: (res) => {
      const name = addName.trim();
      if (editingId) toast({ tone: "success", title: `Updated ${name}`, message: res.reachable === false ? "Saved — still not reachable." : "Saved." });
      else addedToast(name, res.reachable);
      resetForm();
    },
    onError: (e) =>
      toast({ tone: "error", title: editingId ? "Couldn't update member" : "Couldn't add to fleet", message: errMsg(e) }),
    onSettled: () => qc.invalidateQueries({ queryKey: queryKeys.fleet }),
  });
  const canAdd = canAddRemote(addName, addUrl);
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
          <>
            {/* Box-runtime knobs (bind interface · ports · discovery · keep-warm) — host-scoped
                box defaults, set right where you manage the fleet (ADR 0047 D8 / 0048). */}
            <QuickSetting
              keys={[
                "network.bind",
                "fleet.port_base",
                "fleet.discovery.port_min",
                "fleet.discovery.port_max",
                "fleet.discovery.mdns",
                "fleet.warm.max",
                "fleet.warm.grace_seconds",
              ]}
              title="Box runtime"
              label="Box runtime settings"
              icon={<Server size={15} />}
            />
            <Button variant="primary" onClick={onNew}>
              <Plus size={15} /> New agent
            </Button>
          </>
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
                  {/* A remote's `running` IS its reachability probe (it has no local process),
                      so an offline remote is "unreachable", not "stopped" — and its dot reads
                      warning, not neutral, since it's a fault to act on rather than an idle agent. */}
                  {(() => {
                    const label = a.running ? "running" : a.remote ? "unreachable" : "stopped";
                    return (
                      <span role="img" title={label} aria-label={label}>
                        <StatusDot
                          status={a.running ? "success" : a.remote ? "warning" : "neutral"}
                          pulse={a.running}
                        />
                      </span>
                    );
                  })()}
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
                    {/* A remote member can't be started/stopped from here, but its URL/token/
                        name ARE editable in place (the id/slug survives) — or unregister it
                        (the remote agent itself is untouched). */}
                    {a.remote ? (
                      <>
                        <Button icon variant="ghost" title="Edit this member's URL, token or name"
                          disabled={submitRemote.isPending}
                          onClick={() => openEdit(a)}>
                          <Pencil size={14} />
                        </Button>
                        <Button icon variant="ghost" title="Remove from this fleet (the remote agent is untouched)"
                          disabled={removeMember.isPending}
                          onClick={() => removeMember.mutate(a)}>
                          <Trash2 size={14} />
                        </Button>
                      </>
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
          <div className="fleet-discover-actions">
            <Button variant="ghost" onClick={scan} disabled={scanning}>
              <Radar size={14} /> {scanning ? "Scanning…" : "Discover agents on the network"}
            </Button>
            {/* Manual add — the only path for a token-gated remote (discovery carries no
                credential) or one on a subnet the scan can't reach. */}
            <Button variant="ghost" onClick={() => (showAdd ? resetForm() : openAdd())} aria-expanded={showAdd}>
              <Link2 size={14} /> Add a remote by URL
            </Button>
          </div>
          {showAdd ? (
            <form
              className="fleet-add-remote"
              onSubmit={(e) => {
                e.preventDefault();
                if (canAdd && !submitRemote.isPending) submitRemote.mutate();
              }}
            >
              {editingId ? <span className="fleet-add-remote-title">Edit remote member</span> : null}
              <label className="field">
                <span>Name</span>
                <Input
                  value={addName}
                  onChange={(e) => setAddName(e.target.value)}
                  placeholder="e.g. ava (letters, digits, - and _)"
                  autoFocus
                />
              </label>
              <label className="field">
                <span>URL</span>
                <Input
                  value={addUrl}
                  onChange={(e) => setAddUrl(e.target.value)}
                  placeholder="http://100.x.y.z:7870"
                />
              </label>
              <label className="field">
                <span>Token{editingId ? "" : " (optional)"}</span>
                <SecretInput
                  value={addToken}
                  onChange={(e) => setAddToken(e.target.value)}
                  placeholder={editingId ? "•••••••• — leave blank to keep the current token" : "the remote's operator token, if it's gated"}
                />
              </label>
              <div className="fleet-add-remote-actions">
                <Button type="button" variant="ghost" onClick={resetForm}>
                  Cancel
                </Button>
                <Button type="submit" variant="primary" disabled={!canAdd || submitRemote.isPending}>
                  {submitRemote.isPending
                    ? editingId
                      ? "Saving…"
                      : "Adding…"
                    : editingId
                      ? "Save changes"
                      : "Add to fleet"}
                </Button>
              </div>
            </form>
          ) : null}
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
