import "../fleet/fleet.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronDown, ChevronUp, Folder, GripVertical, KeyRound, Link2, Pencil, Play, Plus, Radar, Server, Square, Star, Trash2, Unlink2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Badge, Button, Card, Empty } from "@protolabsai/ui/primitives";
import { Alert, StatusDot } from "@protolabsai/ui/data";
import { Checkbox, DropdownSelect, EditableText, Input, SecretInput, Switch } from "@protolabsai/ui/forms";
import type { DropdownSelectOption } from "@protolabsai/ui/forms";
import { ConfirmDialog, useToast } from "@protolabsai/ui/overlays";
import { PanelHeader } from "@protolabsai/ui/navigation";

import { PairRemoteDialog, type PairResult, type PairTarget } from "./PairRemoteDialog";
import { QuickSetting } from "./QuickSetting";
import {
  delegateLinkFor,
  hubUrlRefusal,
  INSECURE_OPT_IN,
  INSECURE_WARNING,
  isInsecureRefusal,
  needsInsecureOptIn,
  remoteAuthBadge,
} from "../lib/agentPairing";
import { agentHref, api, currentSlug } from "../lib/api";
import { errMsg } from "../lib/format";
import { fleetQuery, queryKeys, settingsSchemaQuery } from "../lib/queries";
import type { DiscoveredAgent, FleetAgent, FleetGroup, FleetStatus, SettingsGroup } from "../lib/types";
import { useIsMobile } from "../lib/useIsMobile";
import { fleetSettingsDisabledReason } from "../app/fleetSettingsGate";

/** The manual add-remote form is submittable only with a name and an http(s) URL. Exported
 * (pure) so the enable rule is unit-tested without rendering the panel. The server does the
 * authoritative validation (charset, SSRF egress, dedupe); this is just the button gate. */
export function canAddRemote(name: string, url: string): boolean {
  return name.trim().length > 0 && /^https?:\/\/.+/.test(url.trim());
}

/** The URL slug is the agent's STABLE id, never its (editable) display name — renaming an
 * agent must not change its URL/bookmarks. `host` is the reserved slug for this instance.
 * Same rule as the topbar FleetSwitcher, which navigates to the same href; exported (pure)
 * so the row link's destination is unit-tested without rendering the panel. */
export const slugOf = (a: { id: string; host?: boolean }) => (a.host ? "host" : a.id);

/** The Box-runtime chip's field set (bind interface · ports · discovery · keep-warm) —
 * every key host-scoped, so the QuickSetting saves to the host layer
 * (ADR 0047 D8 / 0048). Exported so the chip's contents are unit-tested without
 * rendering the panel. */
export const BOX_RUNTIME_KEYS: string[] = [
  "network.bind",
  "fleet.port_base",
  "fleet.discovery.port_min",
  "fleet.discovery.port_max",
  "fleet.discovery.mdns",
  "fleet.warm.max",
  "fleet.warm.grace_seconds",
];

/** Resolve the host's durable autostart roster from the settings schema response. */
export function fleetAutostartRoster(groups: SettingsGroup[] | undefined): string[] {
  const value = groups?.flatMap((g) => g.fields).find((f) => f.key === "fleet.autostart")?.value;
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}

/** A legacy name entry and the preferred stable-id entry both mean this row is enabled. */
export function memberAutostarts(roster: string[], agent: Pick<FleetAgent, "id" | "name">): boolean {
  return roster.includes(agent.id) || roster.includes(agent.name);
}

/** Toggle one row without disturbing entries for members no longer present in the roster. */
export function updateAutostartRoster(
  roster: string[],
  agent: Pick<FleetAgent, "id" | "name">,
  enabled: boolean,
): string[] {
  const others = roster.filter((entry) => entry !== agent.id && entry !== agent.name);
  return enabled ? [...others, agent.id] : others;
}

// ── Manual roster reordering (#3197) — the pure ordering core ─────────────────────────
// The API contract is the COMPLETE immutable-id permutation of the roster (host + local +
// remote): stable ids only, never the editable name/label, so a rename can't perturb order.
// These helpers are exported (pure) so drag math, the move-control payload, boundary/busy
// disabling, accessible labels and the failure-safe reconciliation are unit-tested without
// rendering the panel.

/** The order PUT to /api/fleet/order — the roster ids in their current display order. */
export function fleetOrderIds<T extends { id: string }>(agents: readonly T[]): string[] {
  return agents.map((a) => a.id);
}

/** Two id orders are identical — used to skip a no-op PUT (a drop back onto the same slot, or
 *  a move that ran off a list boundary). */
export function sameOrder(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((id, i) => id === b[i]);
}

/** Whether a full-order PUT should fire: the order must actually CHANGE, and no reorder save may
 *  already be in flight. The single guard for BOTH reorder paths (drag + move controls) — a second
 *  concurrent PUT could complete out of order, or an earlier failure's roll-back could overwrite a
 *  later drag's intended order, so a pending save blocks the next submission entirely (the drag
 *  handle and the move buttons also disable while pending, this is the shared backstop). */
export function shouldSubmitOrder(next: string[], current: string[], pending: boolean): boolean {
  return !pending && !sameOrder(next, current);
}

/** Move the id at `index` one slot up/down, returning a NEW array — or the SAME order when the
 *  move runs off either end (the caller then skips the PUT via `sameOrder`). The move-up /
 *  move-down controls' payload: the accessible, non-pointer reorder path. */
export function moveInList(ids: string[], index: number, dir: "up" | "down"): string[] {
  const target = dir === "up" ? index - 1 : index + 1;
  if (index < 0 || index >= ids.length || target < 0 || target >= ids.length) return ids;
  const next = ids.slice();
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

/** Drag `from` → `to`: pull the dragged id out and re-insert it at the target slot, returning a
 *  NEW array — or the SAME order for a self-drop / out-of-range index. The drag-and-drop payload. */
export function reorderByDrag(ids: string[], from: number, to: number): string[] {
  if (from === to || from < 0 || to < 0 || from >= ids.length || to >= ids.length) return ids;
  const next = ids.slice();
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}

/** Can the row at `index` still move in `dir`? False at the matching list boundary — the
 *  keyboard path announces the boundary rather than moving silently (`moveAnnouncement`). */
export function canMove(index: number, dir: "up" | "down", length: number): boolean {
  return dir === "up" ? index > 0 : index < length - 1;
}

/** The reorder handle's accessible name. It is the ONE reorder control on a row now — the pair
 *  of always-rendered move-up / move-down buttons was retired (#3197 follow-up) because a fixed
 *  80px control column on every row bought two clicks' worth of affordance and cost the roster's
 *  breathing room. The keys therefore travel IN the name (plus `aria-keyshortcuts` and the visible
 *  tooltip): with no buttons on screen, nothing else tells a keyboard user the path exists. Uses
 *  the DISPLAY name (label ?? name), never the id. */
export function reorderLabel(agent: Pick<FleetAgent, "name" | "label">): string {
  return `Reorder ${agent.label ?? agent.name} — drag, or press the up and down arrow keys`;
}

/** What the polite live region says after an arrow-key press. A keyboard reorder moves a row the
 *  user cannot see move, and a press at a list boundary is a no-op — both are silent without this,
 *  which is the one failure a visual review never catches. `index`/`dir` are the move as REQUESTED
 *  (pre-move), so the target slot is derived here. */
export function moveAnnouncement(
  agent: Pick<FleetAgent, "name" | "label">,
  index: number,
  dir: "up" | "down",
  length: number,
): string {
  const display = agent.label ?? agent.name;
  if (!canMove(index, dir, length)) return `${display} is already ${dir === "up" ? "first" : "last"}.`;
  const to = dir === "up" ? index - 1 : index + 1;
  return `Moved ${display} to position ${to + 1} of ${length}.`;
}

/** Reconcile a target id order against the live roster WITHOUT losing a row: rank the agents to
 *  match `order`, drop ids no longer present, and append any agent `order` omits (a member added
 *  or removed between the optimistic write and the server's echo) at the tail. Backs the
 *  optimistic cache write AND keeps every existing row + its actions across a success or a
 *  failure roll-back, so the polling `queryKeys.fleet` stays authoritative. */
export function orderAgentsByIds<T extends { id: string }>(agents: T[], order: string[]): T[] {
  const byId = new Map(agents.map((a) => [a.id, a]));
  const ranked: T[] = [];
  for (const id of order) {
    const a = byId.get(id);
    if (a) {
      ranked.push(a);
      byId.delete(id);
    }
  }
  for (const a of agents) if (byId.has(a.id)) ranked.push(a); // never drop a live row
  return ranked;
}

// ── Fleet settings group editor (docs/design/fleet-switcher-groups.md) ───────────────────────
// Operator-defined switcher sections, edited as a card per group (plus a "No group" card and a
// read-only "Remote" card). The pure core — the name rules + their copy, the delete confirm's
// wording, the picker options, the per-agent home key and the count label — is exported so it is
// unit-tested without the DOM, exactly as the roster-reorder core above is. Group reorder reuses
// the generic `fleetOrderIds` / `moveInList` / `reorderByDrag` / `canMove` / `orderAgentsByIds`
// helpers (a group is just `{ id }`), so the drag + Move up/down math has one tested home.

/** The name field's limit (spec: 1–40 characters). */
export const GROUP_NAME_MAX = 40;

/** Which editor card an agent lives in: remote machines are grouped automatically, a group id no
 *  current group has is treated as ungrouped (same rule as the switcher's `homeOf`). */
export function groupHomeKey(a: Pick<FleetAgent, "remote" | "group">, groupIds: Set<string>): string {
  if (a.remote) return "remote";
  return a.group && groupIds.has(a.group) ? a.group : "ungrouped";
}

/** The spec's rename rules: 1–40 chars, trimmed, unique ignoring case; "Remote" is reserved.
 *  Returns the spec's error copy, or `null` when the name is acceptable. `exceptId` excludes the
 *  group being renamed from the duplicate check. Copy is verbatim from the spec (and matches the
 *  switcher's `validateGroupName`, so the two surfaces reject identically). */
export function groupNameError(raw: string, groups: readonly { id: string; name: string }[], exceptId?: string): string | null {
  const name = raw.trim();
  if (!name) return "Give the group a name.";
  if (name.toLowerCase() === "remote") return '"Remote" is reserved for agents on paired machines.';
  if (groups.some((g) => g.id !== exceptId && g.name.toLowerCase() === name.toLowerCase()))
    return `There's already a group called "${name}".`;
  return null;
}

/** "N agent" / "N agents" — the card count Badge and the delete confirm both read off it. */
export function pluralAgents(n: number): string {
  return `${n} agent${n === 1 ? "" : "s"}`;
}

/** The delete confirm's title + body, verbatim from the approved preview: the agents move to No
 *  group, none is removed. A group with no agents says so instead of "Its 0 agents move…". */
export function deleteGroupConfirm(name: string, count: number): { title: string; body: string } {
  return {
    title: `Delete the group "${name}"?`,
    body: count
      ? `Its ${pluralAgents(count)} move to No group. No agents are removed.`
      : "It has no agents. No agents are removed.",
  };
}

/** The Group picker's options: "No group" (empty value) then every group in switcher order. The
 *  empty value maps back to `null` on assign (clear the group). */
export function groupPickerOptions(groups: readonly { id: string; name: string }[]): DropdownSelectOption[] {
  return [{ value: "", label: "No group" }, ...groups.map((g) => ({ value: g.id, label: g.name }))];
}

/** A first unused "New group" / "New group N" default so Add group lands a usable, unique name the
 *  operator can immediately overtype (the field opens focused). */
export function defaultGroupName(groups: readonly { id: string; name: string }[]): string {
  let name = "New group";
  for (let n = 2; groupNameError(name, groups) !== null; n++) name = `New group ${n}`;
  return name;
}

// Fleet manager (ADR 0042) — Settings → Agents. Lists the workspace agents with live
// status (the query polls every 3s, so a crashed agent flips to stopped on its own) and
// per-row start / stop / remove. "+ New agent" opens the archetype picker via `onNew`.
export function FleetManagerPanel({ onNew, onGroups }: { onNew?: () => void; onGroups?: () => void }) {
  const qc = useQueryClient();
  const fleet = useQuery(fleetQuery());
  // fleet.autostart belongs to the HUB's host layer. Force both this read and the
  // mutation below to the hub even when the panel is open in a member window.
  const hostSettings = useQuery(settingsSchemaQuery(true));
  // The id (never the display name) of the row currently being acted on. Every fleet call
  // below addresses agents by id for the same reason: display names are editable and — since
  // a member can rename ITSELF from its own Identity panel, where sibling names aren't
  // knowable — no longer guaranteed unique. The routes resolve id-first, so an id can never
  // act on the wrong agent.
  const [busy, setBusy] = useState<string | null>(null);
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
  const orderIds = fleetOrderIds(agents); // the complete immutable-id order (drag + move payload)
  const autostartRoster = fleetAutostartRoster(hostSettings.data?.groups);
  const autostartAvailable = Boolean(
    hostSettings.data?.groups.flatMap((group) => group.fields).some((field) => field.key === "fleet.autostart"),
  );
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

  const autostart = useMutation({
    mutationFn: async ({ agent, enabled }: { agent: FleetAgent; enabled: boolean }) => {
      const next = updateAutostartRoster(autostartRoster, agent, enabled);
      const result = await api.saveSettings({ "fleet.autostart": next }, "host", true);
      if (!result.ok) throw new Error(result.messages.join(" · ") || "The setting was not applied.");
      return next;
    },
    onSuccess: (next) => {
      qc.setQueryData<{ groups: SettingsGroup[] }>(settingsSchemaQuery(true).queryKey, (current) =>
        current
          ? {
              groups: current.groups.map((group) => ({
                ...group,
                fields: group.fields.map((field) =>
                  field.key === "fleet.autostart" ? { ...field, value: next } : field,
                ),
              })),
            }
          : current,
      );
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't update autostart", message: errMsg(e) }),
    onSettled: () => qc.invalidateQueries({ queryKey: settingsSchemaQuery(true).queryKey }),
  });
  const act = (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    run.mutate(fn);
  };

  // Agent-to-agent flows (ADR 0042 + 0025): add another agent as a `delegate_to` target of the
  // FOCUSED agent (this window's slug), pointing at its A2A endpoint — then this agent can
  // delegate work to it. The delegates query/POST is slug-scoped, so it lands on the focused agent.
  const delegatesQ = useQuery({ queryKey: queryKeys.delegates, queryFn: () => api.delegates(), retry: false });
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
      qc.invalidateQueries({ queryKey: queryKeys.delegates });
      void delegatesQ.refetch();
    },
  });
  // The inverse gesture (#2266): unlink a member the focused agent already delegates to.
  // Hits the same slug-scoped registry the add does, so the removal lands on the focused
  // agent too — no detour to Settings → Capabilities → Delegates to undo a one-click add.
  // Like the add, the badge flipping back to the link button IS the success feedback.
  const removeDelegate = useMutation({
    mutationFn: (name: string) => api.deleteDelegate(name),
    onError: (e) => toast({ tone: "error", title: "Couldn't remove delegate", message: errMsg(e) }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queryKeys.delegates });
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

  // Which member URLs this window may write as delegates (see `delegateLinkFor`): every `a2a`
  // in /api/fleet resolves on the HUB's box, so it's only valid when the focused agent is the
  // hub or a local member. null = allowed; else the reason the gesture is disabled.
  const linkRefusal = hubUrlRefusal(agents, slug);

  // Network discovery (ADR 0042 §I) — scan the box + LAN for OTHER protoAgents (not in this
  // fleet). Pair… is the primary way in (ADR 0113): it adds the agent as a member holding a
  // paired token, and optionally links it as a delegate through the hub's proxy.
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

  // Pair… / Re-pair (ADR 0113 D1). One dialog for both: a discovered row opens it with an
  // editable URL; a remote row re-pairs its own URL (the hub re-tokens that member in place).
  const [pairTarget, setPairTarget] = useState<PairTarget | null>(null);
  const [pairOffersDelegate, setPairOffersDelegate] = useState(false);
  const openPair = (d: DiscoveredAgent, withDelegate = false) => {
    setPairOffersDelegate(withDelegate && linkRefusal === null);
    setPairTarget({ mode: "pair", url: d.url, label: d.name, name: d.name });
  };
  const openRepair = (a: FleetAgent) => {
    setPairOffersDelegate(false);
    setPairTarget({ mode: "repair", url: a.url ?? "", label: a.label ?? a.name });
  };
  const onPaired = async (res: PairResult, opts: { addDelegate: boolean }) => {
    // It's a member now — out of the found list at once (the rescan would take a round-trip,
    // and until then its Pair… would re-claim against a member that already exists).
    setDiscovered((list) => (list ? list.filter((x) => x.url !== res.agent?.url) : list));
    await qc.invalidateQueries({ queryKey: queryKeys.fleet });
    if (!opts.addDelegate || !res.agent) return;
    // Pair-then-link: the delegate URL must be the member's `a2a` from /api/fleet — the
    // hub's loopback proxy (ADR 0113 D4), which carries the PAIRED token — never the remote's
    // raw `<url>/a2a`, which has no token and 401s. Read it from a fresh roster.
    try {
      const roster = (await api.fleet()).agents;
      const member = roster.find((m) => m.id === res.agent.id);
      const link = member ? delegateLinkFor(roster, slug, member) : null;
      if (member && link?.url) addDelegate.mutate({ name: member.name, url: link.url });
      else toast({ tone: "warning", title: "Paired, but not linked", message: link?.reason ?? "The new member isn't in the fleet list yet." });
    } catch (e) {
      toast({ tone: "error", title: "Couldn't add delegate", message: errMsg(e) });
    }
  };

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
    onSuccess: (res, d) => {
      addedToast(d.name, res.reachable);
      // Drop it from the found list HERE, not just on the re-scan below: the rescan is a
      // round-trip, and until it lands the agent sits in BOTH lists — offering an "Add to
      // this fleet" button that now 400s ("an agent named … already exists"). The rescan
      // stays as the authoritative refresh; this is what the operator sees immediately.
      setDiscovered((list) => (list ? list.filter((x) => x.url !== d.url) : list));
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't add to fleet", message: errMsg(e) }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: queryKeys.fleet });
      void scan(); // authoritative: the new member drops out of the discover list
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
  // ADR 0113 D10 — a TOKEN sent over plain http to a non-loopback, non-tailnet host needs the
  // operator's explicit opt-in (same rule and words as the Pair dialog). Revealed after the
  // hub's own 400 for an http name this browser couldn't classify.
  const [addAllowInsecure, setAddAllowInsecure] = useState(false);
  const [addInsecureRevealed, setAddInsecureRevealed] = useState(false);
  const resetForm = () => {
    setShowAdd(false);
    setEditingId(null);
    setAddName("");
    setAddUrl("");
    setAddToken("");
    setAddAllowInsecure(false);
    setAddInsecureRevealed(false);
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
      const insecure = addAskInsecure && addAllowInsecure ? { allow_insecure: true } : {};
      // On edit, a blank token means "keep" (omit it); on add, pass it through.
      return editingId
        ? api.updateRemoteAgent(editingId, { name, url, ...(token ? { token } : {}), ...insecure })
        : api.addRemoteAgent({ name, url, token, ...insecure });
    },
    onSuccess: (res) => {
      const name = addName.trim();
      if (editingId && "token_cleared" in res && res.token_cleared)
        // The address moved to another host: the hub dropped the token the OLD host issued
        // rather than present it to the new one (ADR 0113). Say so — the member now 401s.
        toast({
          tone: "warning",
          title: `Updated ${name}`,
          message: "New address, so its stored token was cleared — pair again or paste a token.",
        });
      else if (editingId) toast({ tone: "success", title: `Updated ${name}`, message: res.reachable === false ? "Saved — still not reachable." : "Saved." });
      else addedToast(name, res.reachable);
      resetForm();
    },
    onError: (e) => {
      if (isInsecureRefusal(e)) setAddInsecureRevealed(true);
      toast({ tone: "error", title: editingId ? "Couldn't update member" : "Couldn't add to fleet", message: errMsg(e) });
    },
    onSettled: () => qc.invalidateQueries({ queryKey: queryKeys.fleet }),
  });
  const addAskInsecure = (addToken.trim() !== "" && needsInsecureOptIn(addUrl)) || addInsecureRevealed;
  const canAdd = canAddRemote(addName, addUrl) && (!addAskInsecure || addAllowInsecure);
  const removeMember = useMutation({
    mutationFn: (a: FleetAgent) => api.removeRemoteAgent(a.id),
    onError: (e) => toast({ tone: "error", title: "Couldn't remove member", message: errMsg(e) }),
    onSettled: () => qc.invalidateQueries({ queryKey: queryKeys.fleet }),
  });

  // Manual roster reordering (#3197) — persist the display order to PUT /api/fleet/order.
  // The order is the COMPLETE immutable-id permutation (host + local + remote); the panel
  // updates the polling query's cache OPTIMISTICALLY (the new order shows at once) and then
  // invalidates `queryKeys.fleet` on BOTH success and failure so GET /api/fleet stays
  // authoritative — a rejected permutation rolls back to the server's order and refetches; an
  // accepted one is refetched and reconciled. Only the ordering changes: no name / URL / token
  // / process state / immutable id is ever touched.
  const [dragId, setDragId] = useState<string | null>(null); // the id being dragged (pointer path)
  const [announcement, setAnnouncement] = useState(""); // the keyboard path's polite live region
  const [overId, setOverId] = useState<string | null>(null); // the row a drag is hovering (the drop marker)
  const reorder = useMutation({
    mutationFn: (order: string[]) => api.reorderFleet(order),
    onMutate: async (order) => {
      await qc.cancelQueries({ queryKey: queryKeys.fleet }); // don't let an in-flight poll clobber the optimistic order
      const prev = qc.getQueryData<FleetStatus>(queryKeys.fleet);
      qc.setQueryData<FleetStatus>(queryKeys.fleet, (cur) =>
        cur ? { ...cur, agents: orderAgentsByIds(cur.agents, order) } : cur,
      );
      return { prev };
    },
    onError: (e, _order, ctx) => {
      if (ctx?.prev) qc.setQueryData(queryKeys.fleet, ctx.prev); // roll the optimistic order back
      toast({ tone: "error", title: "Couldn't reorder the fleet", message: errMsg(e) });
    },
    onSettled: () => qc.invalidateQueries({ queryKey: queryKeys.fleet }),
  });
  const submitOrder = (order: string[]) => {
    // The single choke point for BOTH reorder paths (drag + move controls): fire only for a real
    // change AND when no save is already in flight — a concurrent full-order PUT could complete out
    // of order, or an earlier failure's roll-back could clobber a later drag's intended order. Skips
    // a no-op PUT (self-drop / boundary move) too.
    if (shouldSubmitOrder(order, orderIds, reorder.isPending)) reorder.mutate(order);
  };
  // The keyboard reorder path (#3197 follow-up): ↑ / ↓ on a focused handle move the row one slot —
  // exactly what the retired move-up / move-down buttons did, one PUT per press through the same
  // `shouldSubmitOrder` guard. The move is ANNOUNCED because nothing else reports it: with the
  // buttons gone there is no control whose disabled state marks a boundary, and a reordered list
  // is silent to a screen reader.
  const moveRow = (agent: FleetAgent, dir: "up" | "down") => {
    // A save already in flight: `shouldSubmitOrder` would drop this press, so return before
    // announcing — otherwise the live region narrates a move that never happens. This is the
    // key path's half of the handle's `aria-disabled`.
    if (reorder.isPending) return;
    const index = orderIds.indexOf(agent.id);
    setAnnouncement(moveAnnouncement(agent, index, dir, orderIds.length));
    submitOrder(moveInList(orderIds, index, dir));
  };
  const dropOnRow = (targetId: string) => {
    if (dragId && dragId !== targetId) {
      submitOrder(reorderByDrag(orderIds, orderIds.indexOf(dragId), orderIds.indexOf(targetId)));
    }
    setDragId(null);
    setOverId(null);
  };

  return (
    <section className="panel stage-panel">
      <PanelHeader
        title="Agents"
        kicker={`${agents.length} agent${agents.length === 1 ? "" : "s"} on this host · the fleet`}
        actions={
          <>
            {/* Box-runtime knobs — host-scoped box defaults, set right where you manage
                the fleet (ADR 0047 D8 / 0048). */}
            <QuickSetting
              keys={BOX_RUNTIME_KEYS}
              title="Box runtime"
              label="Box runtime settings"
              icon={<Server size={15} />}
            />
            {/* The group editor (docs/design/fleet-switcher-groups.md): organize the switcher into
                sections. A sibling view of the roster, so it rides the same FleetSurface toggle. */}
            {onGroups ? (
              <Button variant="default" onClick={onGroups}>
                <Folder size={15} /> Groups
              </Button>
            ) : null}
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
        {/* The keyboard reorder path's announcer (#3197). Visually hidden, but clipped rather than
            display:none — the latter drops the node from the accessibility tree and silences the
            live region, the one failure a visual review can never catch (same argument as the
            palette's `.pa-cmdk__sr`). */}
        <p className="fleet-reorder-status" role="status" aria-live="polite">
          {announcement}
        </p>
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
              const isActive = slugOf(a) === slug; // slug = stable id, not name
              return (
                <li
                  key={a.id}
                  className={`fleet-row${isActive ? " active" : ""}${dragId === a.id ? " dragging" : ""}${
                    overId === a.id && dragId && dragId !== a.id ? " drop-target" : ""
                  }`}
                  // Every roster row is a drop slot (the pinned host included, so a member can be
                  // dropped above it); only non-host rows carry a drag handle to START a drag.
                  onDragOver={(e) => {
                    if (!dragId || dragId === a.id) return;
                    e.preventDefault(); // preventDefault marks a valid drop target
                    e.dataTransfer.dropEffect = "move"; // a move cursor, not the default copy "+"
                    // Mark the slot the drop would land in. Without it a drag is invisible past the
                    // ghost: nothing on the page says WHERE releasing the mouse puts the row.
                    if (overId !== a.id) setOverId(a.id);
                  }}
                  onDrop={() => dropOnRow(a.id)}
                >
                  {/* Manual roster reordering (#3197): ONE control per member row — a grip that
                      is both the pointer drag handle (native HTML5 DnD; the row is the drop target)
                      and the keyboard path (focus it, press ↑ / ↓). It is a real <button>, not the
                      aria-hidden span it started as, because the always-visible move-up / move-down
                      button pair it replaced was the row's only non-pointer affordance and cost a
                      fixed 80px column on every row. The host ("this instance") is pinned — it
                      carries no handle, but stays a valid drop slot. The DS bundles dnd-kit yet
                      exposes it only as AppShell-rail + TabBar reorder (rail/tab-shaped; TabBar's
                      keyboard path is unwired) — no generic sortable-list primitive fits a rich
                      fleet row, so this stays inline: native DnD, no new dependency, no bespoke
                      REUSABLE component. Gap logged in docs/design/ui-component-audit.md. */}
                  <div className="fleet-reorder">
                    {a.host ? null : (
                      <button
                        type="button"
                        className="fleet-drag-handle"
                        // A reorder save already in flight blocks the drag path (and, via
                        // `shouldSubmitOrder`, the key path): a second concurrent full-order PUT
                        // could complete out of order, or an earlier failure's roll-back could
                        // clobber this drag's intended order. `aria-disabled`, NOT `disabled` — a
                        // disabled button drops focus, which would strand a keyboard user mid-
                        // reorder on the one control the row still has.
                        draggable={!reorder.isPending}
                        aria-disabled={reorder.isPending || undefined}
                        aria-label={reorderLabel(a)}
                        aria-keyshortcuts="ArrowUp ArrowDown"
                        title={reorder.isPending ? "Saving the new order…" : "Drag to reorder — or focus and press ↑ / ↓"}
                        onDragStart={(e) => {
                          if (reorder.isPending) {
                            e.preventDefault(); // belt-and-suspenders: draggable={false} already blocks it
                            return;
                          }
                          // Drag the ROW, not the handle. Every engine defaults the drag image to
                          // the dragged element, and the dragged element here is an 18px grip — a
                          // ghost that small reads as "nothing is happening" even when the drag is
                          // working, which is exactly how this landed as "DnD is broken".
                          const row = e.currentTarget.closest("li");
                          if (row) {
                            const box = row.getBoundingClientRect();
                            e.dataTransfer.setDragImage(row, e.clientX - box.left, e.clientY - box.top);
                          }
                          // Firefox refuses to START a drag whose dataTransfer carries no data, and
                          // every engine wants a declared effect for the right cursor. The payload
                          // is the immutable id; the drop reads React state, not this, but a
                          // well-formed transfer is what makes the gesture work off-Chromium.
                          e.dataTransfer.setData("text/plain", a.id);
                          e.dataTransfer.effectAllowed = "move";
                          setDragId(a.id);
                        }}
                        onDragEnd={() => {
                          setDragId(null);
                          setOverId(null); // a drag abandoned outside any row still clears the marker
                        }}
                        onKeyDown={(e) => {
                          if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
                          e.preventDefault(); // otherwise the settings dialog scrolls under the row
                          moveRow(a, e.key === "ArrowUp" ? "up" : "down");
                        }}
                      >
                        <GripVertical size={14} />
                      </button>
                    )}
                  </div>
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
                      ) : isActive ? (
                        (a.label ?? a.name) // you're already here — a link to this window would be a no-op reload
                      ) : (
                        // #2240 — the name is the click-through to that agent's own window. A real
                        // <a href>, not a click handler, so cmd/middle-click opens it in a NEW window
                        // — the point of slug routing (ADR 0042) being one window per agent. Same
                        // destination the topbar FleetSwitcher navigates to; a stopped agent resumes
                        // on arrival via `activateSlugAgent`, so "not currently open" is not a gate.
                        <a className="fleet-name-link" href={agentHref(slugOf(a))} title={`Open ${a.label ?? a.name}`}>
                          {a.label ?? a.name}
                        </a>
                      )}
                      {a.host ? <Badge status="neutral">this instance</Badge> : null}
                      {a.remote ? (
                        <span title="A remote fleet member — proxied by URL">
                          <Badge status="neutral">remote</Badge>
                        </span>
                      ) : null}
                      {/* The hub's token verdict (ADR 0113 D5): a revoked hub shows up as
                          revoked here, instead of a green dot that 401s when clicked. */}
                      {a.remote
                        ? (() => {
                            const badge = remoteAuthBadge(a.auth);
                            return badge ? (
                              <span title={badge.title} data-testid="fleet-auth-badge" data-auth={a.auth}>
                                <Badge status={badge.status}>{badge.label}</Badge>
                              </span>
                            ) : null;
                          })()
                        : null}
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
                    {/* Boot autostart is a hub-owned process policy, so it applies only to
                        local workspace members — never the hub itself or a remote peer. */}
                    {!a.host && !a.remote ? (
                      <Switch
                        className="fleet-autostart-toggle"
                        checked={memberAutostarts(autostartRoster, a)}
                        disabled={!autostartAvailable || autostart.isPending}
                        onCheckedChange={(enabled) => autostart.mutate({ agent: a, enabled })}
                        label="Start on boot"
                      />
                    ) : null}
                    {/* Add as a delegate of the focused agent → enables delegate_to flows. Any
                        agent but the one you're on (it can't delegate to itself). */}
                    {!isActive ? (
                      delegateNames.has(a.name) ? (
                        // The badge is the STATE, the button is the ACTION — the pair keeps the
                        // add/remove symmetric with the link button it replaced. Unlink2 (not
                        // Trash2) on purpose: the row's Trash2 removes the AGENT, and these two
                        // destructive-looking icons sit inches apart (#2266).
                        <>
                          <span title="A delegate of this agent">
                            <Badge status="info">delegate</Badge>
                          </span>
                          <Button icon variant="ghost" title="Remove as a delegate of this agent (delegate_to)"
                            disabled={removeDelegate.isPending}
                            onClick={() => removeDelegate.mutate(a.name)}>
                            <Unlink2 size={14} />
                          </Button>
                        </>
                      ) : (
                        // The URL comes from `delegateLinkFor`: the member's hub-side `a2a` (a
                        // remote's is the hub's proxy, carrying its paired token — ADR 0113 D4),
                        // and ONLY when this window's agent shares the hub's box. From a remote's
                        // window the button stays visible but disabled, and says why.
                        (() => {
                          const link = delegateLinkFor(agents, slug, a);
                          return (
                            <span title={linkRefusal ?? undefined}>
                              <Button icon variant="ghost"
                                title={linkRefusal ?? "Add as a delegate of this agent (delegate_to)"}
                                disabled={addDelegate.isPending || !link.url}
                                onClick={() => link.url && addDelegate.mutate({ name: a.name, url: link.url })}>
                                <Link2 size={14} />
                              </Button>
                            </span>
                          );
                        })()
                      )
                    ) : null}
                    {/* A remote member can't be started/stopped from here, but its URL/token/
                        name ARE editable in place (the id/slug survives) — or unregister it
                        (the remote agent itself is untouched). */}
                    {a.remote ? (
                      <>
                        {/* Re-pair: a fresh code from the remote re-tokens this member in place
                            (id, slug and windows kept). Labelled "Pair…" when nothing is stored
                            yet; promoted to a visible text button when the token is refused, the
                            one state where it's the fix rather than an option. */}
                        <Button
                          variant="ghost"
                          size="sm"
                          title={a.auth === "none" ? "Pair with a code from the remote" : "Re-pair with a fresh code from the remote"}
                          disabled={!a.url}
                          onClick={() => openRepair(a)}
                        >
                          <KeyRound size={14} aria-hidden /> {a.auth === "none" ? "Pair…" : "Re-pair"}
                        </Button>
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
                    {/* The agent serving THIS console can't act on itself — its display name
                        is edited in Settings → Identity instead. On the host console that's
                        the host row; in a sister agent's window (where this panel now also
                        renders) it's that member's row, so the guard keys on the focused slug
                        as well as `host` — otherwise "Stop"/"Remove" would kill or delete the
                        very agent serving the window you're in. */}
                    {a.host || a.remote || isActive ? null : (
                      <>
                        <Button icon variant="ghost" title="Rename (display name only — the id/URL stays)"
                          disabled={rename.isPending}
                          onClick={() => setRenaming(a.id)}>
                          <Pencil size={14} />
                        </Button>
                        {a.running ? (
                          <Button icon variant="ghost" title="Stop" disabled={busy === a.id}
                            onClick={() => act(a.id, () => api.stopAgent(a.id))}>
                            <Square size={14} />
                          </Button>
                        ) : (
                          <Button icon variant="ghost" title="Start" disabled={busy === a.id}
                            onClick={() => act(a.id, () => api.startAgent(a.id))}>
                            <Play size={14} />
                          </Button>
                        )}
                        <Button icon variant="ghost" title="Remove" disabled={busy === a.id}
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
            {/* Manual add — a remote on a subnet the scan can't reach, or a pasted operator
                token for an instance that predates pairing (ADR 0113 D3). Pair… on a
                discovered row is the primary path for a token-gated protoAgent. */}
            {/* Pair by URL — a remote the scan can't see (another subnet, mDNS off, a headless
                box that printed its code with `protoagent pair`). */}
            <Button
              variant="ghost"
              onClick={() => {
                setPairOffersDelegate(false);
                setPairTarget({ mode: "pair", url: "", label: "a remote agent" });
              }}
            >
              <KeyRound size={14} /> Pair by URL…
            </Button>
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
                  onChange={(e) => {
                    setAddUrl(e.target.value);
                    setAddAllowInsecure(false); // consent is per host
                    setAddInsecureRevealed(false);
                  }}
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
              {addAskInsecure ? (
                <Alert status="warning" className="fleet-insecure">
                  <p>{INSECURE_WARNING}</p>
                  <Checkbox
                    checked={addAllowInsecure}
                    onCheckedChange={setAddAllowInsecure}
                    required
                    label={INSECURE_OPT_IN}
                    data-testid="fleet-add-insecure"
                  />
                </Alert>
              ) : null}
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
                  // `--found` marks a DISCOVERY result, not a member. The two lists render
                  // the same row shape, so without a modifier a `.fleet-row` selector (CSS
                  // or e2e) can't tell "on the network" from "in the fleet" — which made
                  // the add-a-remote spec strict-mode-flake the moment both were on screen.
                  <li key={d.url} className="fleet-row fleet-row--found">
                    <StatusDot status="success" />
                    <div className="fleet-row-main">
                      <span className="fleet-name">{d.name}</span>
                      <span className="fleet-meta">{d.url}</span>
                    </div>
                    <div className="fleet-row-actions">
                      {/* Pair… is the PRIMARY way in (ADR 0113): the remote shows a code, this
                          hub redeems it, and the member holds its own revocable token. "Add to
                          this fleet" stays as the tokenless path for an OPEN remote. */}
                      <Button variant="primary" size="sm" title="Pair with a code from that agent (Settings ▸ Devices ▸ Pair an agent)"
                        onClick={() => openPair(d)}>
                        <KeyRound size={14} aria-hidden /> Pair…
                      </Button>
                      <Button icon variant="ghost" title="Add to this fleet without a token (a switchable remote member — for an open remote)"
                        disabled={addMember.isPending}
                        onClick={() => addMember.mutate(d)}>
                        <Plus size={14} />
                      </Button>
                      {/* Delegate: pair FIRST, then link through the hub's proxy. The old direct
                          `<url>/a2a` delegate carried no token, so against any paired (gated)
                          agent it 401'd on first use — the failure ADR 0113 D4 removes. The same
                          button now opens Pair… with "also add as a delegate" on. */}
                      {delegateNames.has(d.name) ? (
                        <span title="A delegate of this agent">
                          <Badge status="info">delegate</Badge>
                        </span>
                      ) : (
                        <span title={linkRefusal ?? undefined}>
                          <Button icon variant="ghost"
                            title={linkRefusal ?? "Pair, then add as a delegate of this agent (delegate_to)"}
                            disabled={linkRefusal !== null}
                            onClick={() => openPair(d, true)}>
                            <Link2 size={14} />
                          </Button>
                        </span>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )
          ) : null}
        </div>
      </div>

      <PairRemoteDialog
        target={pairTarget}
        offerDelegate={pairOffersDelegate}
        onClose={() => setPairTarget(null)}
        onPaired={(res, opts) => void onPaired(res, opts)}
      />

      <ConfirmDialog
        open={confirmRemove !== null}
        title={confirmRemove ? `Remove ${confirmRemove.name}?` : ""}
        confirmLabel={purge ? "Remove + purge data" : "Remove"}
        destructive
        onConfirm={() => {
          const a = confirmRemove;
          const wipe = purge;
          setConfirmRemove(null);
          if (a) act(a.id, () => api.removeAgent(a.id, wipe));
        }}
        onClose={() => setConfirmRemove(null)}
      >
        {/* Say which of the two this actually is. The switch used to change nothing — the
            workspace dir (which IS a member's whole instance root: config, chats, knowledge,
            tasks) was deleted either way, so "Remove" quietly did what "purge" advertised
            (#2384). Now Remove retires the agent and leaves every byte on disk. */}
        <p>
          {purge
            ? "Stops the agent, removes it from the fleet, and deletes its workspace — chats, knowledge, tasks and config. This cannot be undone."
            : "Stops the agent and removes it from the fleet. Its workspace stays on disk, so nothing is lost."}
        </p>
        <Switch
          className="fleet-purge"
          checked={purge}
          onCheckedChange={setPurge}
          label="Also delete its workspace data (irreversible)"
        />
      </ConfirmDialog>
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────────────────────────────────
// Fleet settings group editor (docs/design/fleet-switcher-groups.md) — Settings ▸ Fleet ▸ Groups.
// A card per operator group in switcher order, then a "No group" card and a read-only "Remote"
// card. Every change saves at once (a small inline "Saved", no Save button). Supports: Add group
// (opens a name field focused), rename inline (Enter saves / Escape cancels, with the spec's error
// copy), reorder (the drag handle AND keyboard-reachable Move up / Move down), delete (the spec's
// confirm), assign an agent (drag between cards OR the per-row Group picker) and pin / unpin any
// agent — remote included. The reorder + assign + pin writes are the SAME hub API the switcher
// uses (one roster field, so a pin here shows in the switcher and vice-versa), optimistically
// patched then reconciled by the 3s poll. All writes are hub-only and gated by
// `fleetSettingsDisabledReason` like the rest of Fleet settings. Phone (below 767px, `useIsMobile`):
// one column, drag OFF (no handles), the Group picker on its own line, Move up / Move down for
// reorder, 44px targets, the confirm fits 390px — all via the `fleet-groups.is-phone` CSS; the
// component itself only drops `draggable`.
export function FleetGroupsPanel({ onBack }: { onBack?: () => void }) {
  const qc = useQueryClient();
  const fleet = useQuery(fleetQuery());
  const phone = useIsMobile();
  const rootRef = useRef<HTMLDivElement>(null);

  const agents = useMemo(() => fleet.data?.agents ?? [], [fleet.data]);
  const groups = useMemo(() => fleet.data?.groups ?? [], [fleet.data]);
  // Non-null only on a spawned member reached DIRECTLY on its own port (#1708/#1999): there is no
  // hub roster to write to, so the editor is read-only there, like Fleet settings' other writes.
  const blocked = fleetSettingsDisabledReason(agents);
  const canDrag = !phone && !blocked; // touch has no reliable drag; the picker is its path

  const groupIds = useMemo(() => new Set(groups.map((g) => g.id)), [groups]);
  const membersOf = useCallback(
    (key: string) => agents.filter((a) => groupHomeKey(a, groupIds) === key),
    [agents, groupIds],
  );

  // ── live-save feedback: a small inline "Saved" per change, no Save button ──
  const [saved, setSaved] = useState("");
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const markSaved = useCallback(() => {
    setSaved("Saved");
    if (savedTimer.current) clearTimeout(savedTimer.current);
    savedTimer.current = setTimeout(() => setSaved(""), 2000);
  }, []);
  useEffect(() => () => {
    if (savedTimer.current != null) clearTimeout(savedTimer.current);
  }, []);

  // ── roster writes (hub, shared by every browser) — optimistic patch then invalidate, exactly as
  // the switcher does. The 3s poll stays authoritative and rolls back a rejected write. ──
  const patchAgent = useCallback(
    (id: string, patch: Partial<FleetAgent>) =>
      qc.setQueryData<FleetStatus>(queryKeys.fleet, (cur) =>
        cur ? { ...cur, agents: cur.agents.map((a) => (a.id === id ? { ...a, ...patch } : a)) } : cur,
      ),
    [qc],
  );
  const patchGroupOrder = useCallback(
    (order: string[]) =>
      qc.setQueryData<FleetStatus>(queryKeys.fleet, (cur) =>
        cur ? { ...cur, groups: orderAgentsByIds(cur.groups ?? [], order) } : cur,
      ),
    [qc],
  );
  const invalidate = useCallback(() => qc.invalidateQueries({ queryKey: queryKeys.fleet }), [qc]);
  const onWrite = useMemo(
    () => ({ onSuccess: markSaved, onSettled: invalidate }),
    [markSaved, invalidate],
  );

  const createMut = useMutation({ mutationFn: (name: string) => api.createFleetGroup(name), ...onWrite });
  const renameMut = useMutation({ mutationFn: (v: { id: string; name: string }) => api.renameFleetGroup(v.id, v.name), ...onWrite });
  const reorderMut = useMutation({ mutationFn: (order: string[]) => api.reorderFleetGroups(order), ...onWrite });
  const deleteMut = useMutation({ mutationFn: (id: string) => api.deleteFleetGroup(id), ...onWrite });
  const assignMut = useMutation({ mutationFn: (v: { id: string; group: string | null }) => api.setFleetAgentGroup(v.id, v.group), ...onWrite });
  const pinMut = useMutation({ mutationFn: (v: { id: string; pinned: boolean }) => (v.pinned ? api.pinFleetAgent(v.id) : api.unpinFleetAgent(v.id)), ...onWrite });

  // ── rename ──
  const [editingId, setEditingId] = useState<string | null>(null);
  const [renameErr, setRenameErr] = useState<string | null>(null);
  const beginRename = useCallback((g: FleetGroup) => {
    setRenameErr(null);
    setEditingId(g.id);
  }, []);
  const cancelRename = useCallback(() => {
    setEditingId(null);
    setRenameErr(null);
  }, []);
  const commitRename = useCallback(
    (g: FleetGroup, raw: string): boolean => {
      const err = groupNameError(raw, groups, g.id);
      if (err) {
        setRenameErr(err); // stay in edit, show the spec's copy, nothing changed
        return false;
      }
      const name = raw.trim();
      if (name !== g.name) renameMut.mutate({ id: g.id, name });
      setEditingId(null);
      setRenameErr(null);
      return true;
    },
    [groups, renameMut],
  );

  // ── add group: create with a unique default, then open its field focused to overtype ──
  const addGroup = useCallback(async () => {
    try {
      const res = await createMut.mutateAsync(defaultGroupName(groups));
      setRenameErr(null);
      setEditingId(res.group.id);
    } catch {
      // A rejected create (e.g. a name race another browser just made) leaves the list unchanged;
      // the invalidation reconciles. The default name is locally unique, so this is rare.
    }
  }, [groups, createMut]);

  // ── reorder (drag handle + Move up / Move down), reusing the roster-reorder core ──
  const focusAfterMove = useRef<{ id: string; dir: "up" | "down" } | null>(null);
  const reorderGroups = useCallback(
    (order: string[]) => {
      if (!shouldSubmitOrder(order, fleetOrderIds(groups), reorderMut.isPending)) return;
      patchGroupOrder(order);
      reorderMut.mutate(order);
    },
    [groups, reorderMut, patchGroupOrder],
  );
  const moveGroup = useCallback(
    (g: FleetGroup, dir: "up" | "down") => {
      const ids = fleetOrderIds(groups);
      const i = ids.indexOf(g.id);
      if (!canMove(i, dir, ids.length) || reorderMut.isPending) return;
      focusAfterMove.current = { id: g.id, dir }; // keep the keyboard path on the control after the move
      reorderGroups(moveInList(ids, i, dir));
    },
    [groups, reorderMut, reorderGroups],
  );
  // A Move up/down at a NEW boundary disables the pressed button (focus would be lost): land focus
  // on it if still enabled, else its sibling — the DS buttons keep stable keys so the others stay put.
  useEffect(() => {
    const want = focusAfterMove.current;
    if (!want || !rootRef.current) return;
    focusAfterMove.current = null;
    const btns = [...rootRef.current.querySelectorAll<HTMLButtonElement>("[data-gmove][data-gid]")].filter(
      (b) => b.dataset.gid === want.id,
    );
    const pick = (d: string) => btns.find((b) => b.dataset.gmove === d);
    const primary = pick(want.dir);
    (primary && !primary.disabled ? primary : pick(want.dir === "up" ? "down" : "up"))?.focus();
  });

  // ── assign (drag into a card's zone, or the per-row Group picker) ──
  const [landedId, setLandedId] = useState<string | null>(null);
  const landTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (landTimer.current != null) clearTimeout(landTimer.current);
  }, []);
  const assign = useCallback(
    (a: FleetAgent, group: string | null) => {
      if (a.remote || (a.group ?? null) === group) return; // remote is automatic; skip a no-op
      patchAgent(a.id, { group });
      setLandedId(a.id); // a brief highlight so the operator sees where it landed
      if (landTimer.current) clearTimeout(landTimer.current);
      landTimer.current = setTimeout(() => setLandedId(null), 1200);
      assignMut.mutate({ id: a.id, group });
    },
    [patchAgent, assignMut],
  );

  // ── pin / unpin (any agent, remote included) ──
  const togglePin = useCallback(
    (a: FleetAgent) => {
      const pinned = !a.pinned;
      patchAgent(a.id, { pinned, pinned_at: pinned ? new Date().toISOString() : undefined });
      pinMut.mutate({ id: a.id, pinned });
    },
    [patchAgent, pinMut],
  );

  // ── delete (the spec's confirm; the agents become ungrouped, none is removed) ──
  const [confirmDelete, setConfirmDelete] = useState<FleetGroup | null>(null);
  const doDelete = useCallback(() => {
    const g = confirmDelete;
    setConfirmDelete(null);
    if (!g) return;
    if (editingId === g.id) cancelRename();
    deleteMut.mutate(g.id);
  }, [confirmDelete, editingId, cancelRename, deleteMut]);

  // ── native HTML5 drag: group cards reorder, agent rows move between cards (desktop only) ──
  const [drag, setDrag] = useState<{ kind: "group" | "agent"; id: string } | null>(null);
  const [dropGroupId, setDropGroupId] = useState<string | null>(null);
  const [dropZone, setDropZone] = useState<string | null>(null);
  const clearDrag = useCallback(() => {
    setDrag(null);
    setDropGroupId(null);
    setDropZone(null);
  }, []);
  const dropGroupBefore = useCallback(
    (targetId: string) => {
      if (drag?.kind === "group" && drag.id !== targetId) {
        const ids = fleetOrderIds(groups);
        reorderGroups(reorderByDrag(ids, ids.indexOf(drag.id), ids.indexOf(targetId)));
      }
      clearDrag();
    },
    [drag, groups, reorderGroups, clearDrag],
  );
  const dropOnZone = useCallback(
    (zoneKey: string) => {
      if (drag?.kind === "agent" && zoneKey !== "remote") {
        const a = agents.find((x) => x.id === drag.id);
        if (a) assign(a, zoneKey === "ungrouped" ? null : zoneKey);
      }
      clearDrag();
    },
    [drag, agents, assign, clearDrag],
  );

  // ── rows ──
  const confirmCopy = confirmDelete ? deleteGroupConfirm(confirmDelete.name, membersOf(confirmDelete.id).length) : null;

  const agentRow = (a: FleetAgent) => {
    const name = a.label ?? a.name;
    const draggable = canDrag && !a.remote;
    return (
      <li
        key={a.id}
        className={`fleet-arow${a.id === landedId ? " landed" : ""}${drag?.kind === "agent" && drag.id === a.id ? " dragging" : ""}`}
        draggable={draggable || undefined}
        onDragStart={
          draggable
            ? (e) => {
                setDrag({ kind: "agent", id: a.id });
                e.dataTransfer.effectAllowed = "move";
                e.dataTransfer.setData("text/plain", a.id);
              }
            : undefined
        }
        onDragEnd={draggable ? clearDrag : undefined}
      >
        <span role="img" aria-label={a.running ? "running" : "stopped"}>
          <StatusDot status={a.running ? "success" : "neutral"} pulse={a.running} />
        </span>
        <span className="fleet-aname">
          <span className="fleet-aname-text">{name}</span>
          {a.host ? <Badge status="neutral">this instance</Badge> : null}
        </span>
        <button
          type="button"
          className={`fleet-star${a.pinned ? " is-pinned" : ""}`}
          aria-pressed={a.pinned ?? false}
          aria-label={`${a.pinned ? "Unpin" : "Pin"} ${name}`}
          title={a.pinned ? "Pinned to the top of the switcher" : "Pin to the top of the switcher"}
          disabled={!!blocked}
          onClick={() => togglePin(a)}
        >
          <Star size={15} fill={a.pinned ? "currentColor" : "none"} aria-hidden />
        </button>
        {a.remote ? (
          // Remote agents are grouped automatically by their machine — read-only, no picker (spec).
          <span className="fleet-remote-where">On {a.machine ?? "a paired machine"}</span>
        ) : (
          <DropdownSelect
            className="fleet-group-picker"
            aria-label={`Group for ${name}`}
            value={a.group && groupIds.has(a.group) ? a.group : ""}
            options={groupPickerOptions(groups)}
            disabled={!!blocked}
            onValueChange={(v) => assign(a, v || null)}
          />
        )}
      </li>
    );
  };

  const zone = (key: string, rows: FleetAgent[], emptyText: string, readOnly = false) => (
    <ul
      className={`fleet-zone${!readOnly && drag?.kind === "agent" && dropZone === key ? " is-over" : ""}`}
      data-zone={key}
      aria-label="Agents"
      onDragOver={
        !readOnly
          ? (e) => {
              if (drag?.kind !== "agent") return;
              e.preventDefault();
              e.dataTransfer.dropEffect = "move";
              if (dropZone !== key) setDropZone(key);
            }
          : undefined
      }
      onDrop={!readOnly ? () => dropOnZone(key) : undefined}
    >
      {rows.length ? rows.map(agentRow) : <li className="fleet-zone-empty">{emptyText}</li>}
    </ul>
  );

  const groupCard = (g: FleetGroup, i: number) => {
    const rows = membersOf(g.id);
    return (
      <Card
        key={g.id}
        className={`fleet-gcard${drag?.kind === "group" && drag.id === g.id ? " dragging" : ""}${
          drag?.kind === "group" && dropGroupId === g.id && drag.id !== g.id ? " drop-before" : ""
        }`}
        aria-label={`${g.name} group`}
        onDragOver={
          canDrag
            ? (e) => {
                if (drag?.kind !== "group") return;
                e.preventDefault();
                if (dropGroupId !== g.id) setDropGroupId(g.id);
              }
            : undefined
        }
        onDrop={canDrag ? () => dropGroupBefore(g.id) : undefined}
      >
        <div className="fleet-ghead">
          {canDrag ? (
            <span
              className="fleet-ghandle"
              draggable
              aria-hidden
              title="Drag to reorder"
              onDragStart={(e) => {
                setDrag({ kind: "group", id: g.id });
                e.dataTransfer.effectAllowed = "move";
                e.dataTransfer.setData("text/plain", g.id);
                const card = (e.currentTarget as HTMLElement).closest(".fleet-gcard");
                if (card) {
                  const box = card.getBoundingClientRect();
                  e.dataTransfer.setDragImage(card, e.clientX - box.left, e.clientY - box.top);
                }
              }}
              onDragEnd={clearDrag}
            >
              <GripVertical size={15} />
            </span>
          ) : null}
          <div className="fleet-gname">
            {editingId === g.id ? (
              <GroupRenameField initial={g.name} error={renameErr} onCommit={(v) => commitRename(g, v)} onCancel={cancelRename} />
            ) : (
              <button type="button" className="fleet-gname-btn" title="Rename" disabled={!!blocked} onClick={() => beginRename(g)}>
                {g.name}
              </button>
            )}
          </div>
          <Badge status="neutral">{pluralAgents(rows.length)}</Badge>
          <Button icon variant="ghost" size="sm" data-gmove="up" data-gid={g.id} aria-label={`Move ${g.name} up`} title="Move up" disabled={i === 0 || !!blocked} onClick={() => moveGroup(g, "up")}>
            <ChevronUp size={15} />
          </Button>
          <Button icon variant="ghost" size="sm" data-gmove="down" data-gid={g.id} aria-label={`Move ${g.name} down`} title="Move down" disabled={i === groups.length - 1 || !!blocked} onClick={() => moveGroup(g, "down")}>
            <ChevronDown size={15} />
          </Button>
          <Button icon variant="ghost" size="sm" aria-label={`Delete ${g.name}`} title="Delete" disabled={!!blocked} onClick={() => setConfirmDelete(g)}>
            <Trash2 size={15} />
          </Button>
        </div>
        {zone(g.id, rows, "No agents yet. Drag one here, or pick this group from an agent's menu.")}
      </Card>
    );
  };

  return (
    <section className="panel stage-panel">
      <PanelHeader
        title={
          <span className="fleet-groups-title">
            Groups
            <span className="fleet-saved" role="status" aria-live="polite">
              {saved}
            </span>
          </span>
        }
        kicker="Groups organize the agent switcher. They don't change what agents can do."
        actions={
          onBack ? (
            <Button variant="default" onClick={onBack}>
              <ArrowLeft size={15} /> Agents
            </Button>
          ) : undefined
        }
      />
      <div className="stage-body">
        {blocked ? (
          <Empty title="Managed from the host instance" description={blocked} />
        ) : fleet.isLoading ? (
          <Empty>Loading the fleet…</Empty>
        ) : (
          <div className={`fleet-groups${phone ? " is-phone" : ""}`} ref={rootRef}>
            <p className="fleet-groups-legend">
              {phone ? "Pick a group from an agent's menu to move it." : "Drag an agent to another group, or pick one from its menu."} The
              star pins an agent to the top of the switcher.
            </p>
            {groups.length === 0 ? (
              <Empty>No groups yet. Add one to sort the switcher into sections.</Empty>
            ) : (
              groups.map((g, i) => groupCard(g, i))
            )}
            <div className="fleet-groups-add">
              <Button variant="default" size="sm" onClick={() => void addGroup()} disabled={createMut.isPending}>
                <Plus size={15} /> Add group
              </Button>
            </div>
            {/* "No group" card — the ungrouped agents that sit near the top of the switcher. */}
            <Card className="fleet-gcard fleet-gcard--fixed" aria-label="No group">
              <div className="fleet-ghead">
                <div className="fleet-gname">
                  <span className="fleet-gname-fixed">No group</span>
                </div>
                <Badge status="neutral">{pluralAgents(membersOf("ungrouped").length)}</Badge>
              </div>
              <p className="fleet-gnote">These sit near the top of the switcher, without a heading.</p>
              {zone("ungrouped", membersOf("ungrouped"), "Every agent has a group.")}
            </Card>
            {/* Remote card — read-only; paired-machine agents are grouped automatically (spec). */}
            <Card className="fleet-gcard fleet-gcard--fixed" aria-label="Remote">
              <div className="fleet-ghead">
                <div className="fleet-gname">
                  <span className="fleet-gname-fixed">Remote</span>
                </div>
                <Badge status="neutral">{pluralAgents(membersOf("remote").length)}</Badge>
              </div>
              <p className="fleet-gnote">Agents from paired machines are grouped automatically.</p>
              {zone("remote", membersOf("remote"), "No paired machines yet.", true)}
            </Card>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmDelete !== null}
        title={confirmCopy?.title ?? ""}
        confirmLabel="Delete group"
        destructive
        onConfirm={doDelete}
        onClose={() => setConfirmDelete(null)}
      >
        <p>{confirmCopy?.body ?? ""}</p>
      </ConfirmDialog>
    </section>
  );
}

// The inline rename field (spec: Enter saves, Escape cancels, blur saves best-effort). Its own draft
// state keeps the parent card re-render cheap; the parent owns the error so an invalid name stays in
// edit with the spec's copy. `stopPropagation` keeps the key events off any ancestor list handlers.
function GroupRenameField({
  initial,
  error,
  onCommit,
  onCancel,
}: {
  initial: string;
  error: string | null;
  onCommit: (value: string) => boolean;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const done = useRef(false); // a resolved Enter/Escape suppresses the unmount blur's re-commit
  return (
    <div className="fleet-grename" onKeyDown={(e) => e.stopPropagation()}>
      <Input
        value={value}
        autoFocus
        autoComplete="off"
        spellCheck={false}
        maxLength={GROUP_NAME_MAX}
        aria-label="Group name"
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? "fleet-grename-err" : undefined}
        className={`fleet-grename-input${error ? " is-invalid" : ""}`}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            if (onCommit(value)) done.current = true;
          } else if (e.key === "Escape") {
            e.preventDefault();
            done.current = true;
            onCancel();
          }
        }}
        onBlur={() => {
          if (done.current) return;
          onCommit(value);
        }}
      />
      {error ? (
        <div className="fleet-grename-err" id="fleet-grename-err" role="alert">
          {error}
        </div>
      ) : null}
    </div>
  );
}
