// The Fleet Room (⌘K, ADR 0042 + the palette-UX overhaul). A native palette morph-view —
// a sibling of PaletteChat — that turns the fleet from a page-load switcher into a
// co-present room: every workspace agent as a presence-aware member you can Open,
// start/stop, or *address in place* (a slug-targeted /api/chat send), plus a one-key
// broadcast to everyone online. Entered from the palette's "Agents" group; the DS
// CommandPalette supplies the back/close chrome + footer, we render the body.
//
// v0 scope: roster + presence + address/broadcast. Deferred to v1 (additive layers on
// this shell): the live fleet-wide activity feed (aggregate each member's event bus,
// ADR 0039) and inline reply transcripts. Ungated for now — it reflects whatever
// api.fleet() returns for this window; host-scoping (cf. fleetSettingsGate) can follow.
import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import { ExternalLink, Play, Radio, Send, Square } from "lucide-react";
import { useToast } from "@protolabsai/ui/overlays";
import type { PaletteContext, PaletteView } from "@protolabsai/ui/command-palette";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, currentSlug } from "../lib/api";
import { fleetQuery, queryKeys } from "../lib/queries";
import { errMsg } from "../lib/format";
import type { FleetAgent } from "../lib/types";
import "./fleet-room.css";

/** The routing slug for a member — the host entry is the reserved "host" (ADR 0042). */
const slugOf = (a: FleetAgent): string => (a.host ? "host" : a.id);

type PresenceKey = "host" | "online" | "remote" | "stopped" | "unreachable";

/** Presence derived from the roster: `running` IS the live reachability probe, `remote`
 *  distinguishes a proxied peer, `host` is the instance serving this console. */
function presenceOf(a: FleetAgent): { key: PresenceKey; label: string } {
  if (a.host) return { key: "host", label: "this instance" };
  if (a.running) return a.remote ? { key: "remote", label: "remote" } : { key: "online", label: "online" };
  return a.remote ? { key: "unreachable", label: "unreachable" } : { key: "stopped", label: "stopped" };
}

const clip = (s: string, n = 72): string => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/** "broadcast" = fan out to every other online member; otherwise a specific member slug. */
type Target = "broadcast" | string;

function FleetRoom({ ctx, onOpenAgent }: { ctx: PaletteContext; onOpenAgent: (slug: string) => void }) {
  const { data: fleet } = useQuery(fleetQuery());
  const qc = useQueryClient();
  const toast = useToast();
  const [target, setTarget] = useState<Target>("broadcast");
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const here = currentSlug();

  useEffect(() => inputRef.current?.focus(), []);

  // Host first, then reachable (running) before down, then alphabetical — deterministic
  // across the 3s poll (React Query structural-shares equal data, so no reorder churn).
  const roster = useMemo(() => {
    const agents = fleet?.agents ?? [];
    return [...agents].sort(
      (a, b) =>
        Number(!!b.host) - Number(!!a.host) ||
        Number(b.running) - Number(a.running) ||
        a.name.localeCompare(b.name),
    );
  }, [fleet]);

  // Broadcast reaches every OTHER online member (never the window you're already in).
  const broadcastTargets = useMemo(
    () => roster.filter((a) => a.running && slugOf(a) !== here),
    [roster, here],
  );

  const open = (a: FleetAgent) => {
    ctx.close();
    onOpenAgent(slugOf(a)); // routed through the palette nav chokepoint (launcher-safe)
  };

  const toggle = (a: FleetAgent) => {
    const on = a.running;
    (on ? api.stopAgent(a.name) : api.startAgent(a.name))
      .then(() => {
        qc.invalidateQueries({ queryKey: queryKeys.fleet });
        toast({
          tone: "success",
          title: on ? `Stopping ${a.name}…` : `Starting ${a.name}…`,
          message: on ? `${a.name} is going offline.` : `${a.name} is coming online.`,
        });
      })
      .catch((e) => toast({ tone: "error", title: "Couldn't toggle agent", message: errMsg(e) }));
  };

  const send = () => {
    const msg = draft.trim();
    if (!msg) return;
    if (target === "broadcast") {
      if (!broadcastTargets.length) {
        toast({ tone: "error", title: "No one to broadcast to", message: "No other members are online." });
        return;
      }
      // Fire-and-forget fan-out — each member runs the turn durably on its own instance.
      for (const a of broadcastTargets) {
        api
          .sendToAgent(slugOf(a), msg)
          .catch((e) => toast({ tone: "error", title: `Couldn't reach ${a.name}`, message: errMsg(e) }));
      }
      toast({
        tone: "success",
        title: `Broadcast to ${broadcastTargets.length} member${broadcastTargets.length > 1 ? "s" : ""}`,
        message: clip(msg),
      });
    } else {
      const a = roster.find((x) => slugOf(x) === target);
      api
        .sendToAgent(target, msg)
        .then(() => toast({ tone: "success", title: `Sent to ${a?.name ?? target}`, message: clip(msg) }))
        .catch((e) => toast({ tone: "error", title: `Couldn't reach ${a?.name ?? target}`, message: errMsg(e) }));
    }
    setDraft("");
    inputRef.current?.focus();
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (e.metaKey || e.ctrlKey) setTarget("broadcast"); // ⌘↵ always broadcasts
      send();
    }
  };

  const targetName =
    target === "broadcast"
      ? `All online · ${broadcastTargets.length}`
      : (roster.find((a) => slugOf(a) === target)?.name ?? target);

  return (
    <div className="flr">
      <div className="flr__list" role="group" aria-label="Fleet members">
        {roster.length === 0 && <div className="flr__empty">No members yet — add one from Settings ▸ Agents.</div>}
        {roster.map((a) => {
          const slug = slugOf(a);
          const p = presenceOf(a);
          const isTarget = target === slug;
          const local = !a.host && !a.remote; // only a local process can be started/stopped here
          return (
            <div key={slug} className={`flr__member${isTarget ? " is-target" : ""}${a.running ? "" : " is-down"}`}>
              <span className={`flr__dot flr__dot--${p.key}`} aria-hidden />
              <button
                type="button"
                className="flr__who"
                onClick={() => setTarget(slug)}
                aria-pressed={isTarget}
                title={`Address ${a.name}`}
              >
                <span className="flr__name">
                  {a.name}
                  {a.host && <span className="flr__tag flr__tag--host">this instance</span>}
                  {a.remote && <span className="flr__tag flr__tag--remote">remote</span>}
                </span>
                <span className="flr__meta">
                  {[a.bundle, a.port ? `:${a.port}` : null, p.label].filter(Boolean).join(" · ")}
                </span>
              </button>
              <div className="flr__actions">
                {local && (
                  <button
                    type="button"
                    className="flr__icon"
                    onClick={() => toggle(a)}
                    title={a.running ? "Stop" : "Start"}
                    aria-label={a.running ? `Stop ${a.name}` : `Start ${a.name}`}
                  >
                    {a.running ? <Square size={14} /> : <Play size={14} />}
                  </button>
                )}
                <button
                  type="button"
                  className="flr__icon"
                  onClick={() => open(a)}
                  disabled={!a.running}
                  title={a.running ? "Open console" : "Offline"}
                  aria-label={`Open ${a.name} console`}
                >
                  <ExternalLink size={14} />
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="flr__composer">
        <button
          type="button"
          className={`flr__target${target === "broadcast" ? " is-cast" : ""}`}
          onClick={() => setTarget("broadcast")}
          title={target === "broadcast" ? "Broadcasting to all online members" : "Switch to broadcast"}
        >
          {target === "broadcast" ? <Radio size={13} /> : null}
          <span>{targetName}</span>
          {target !== "broadcast" && (
            <span className="flr__target-x" aria-hidden>
              ×
            </span>
          )}
        </button>
        <input
          ref={inputRef}
          className="flr__input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={target === "broadcast" ? "Message everyone online…" : `Message ${targetName}…`}
          aria-label="Message"
        />
        <button type="button" className="flr__send" onClick={send} disabled={!draft.trim()} aria-label="Send">
          <Send size={15} />
        </button>
      </div>
    </div>
  );
}

/** The palette view (registered by usePaletteRegistry; entered by the "Fleet Room"
 *  command). Kept as a factory so the JSX lives here and usePaletteRegistry stays .ts.
 *  `onOpenAgent` routes through the registry's nav chokepoint so it also works forwarded
 *  from the frameless desktop launcher window (ADR 0057). */
export function fleetRoomView(opts: { onOpenAgent: (slug: string) => void }): PaletteView {
  return {
    id: "fleet-room",
    title: "Fleet",
    width: 680,
    footerHint: (
      <span className="flr__hint">
        <b>↵</b> send · click a member to address · <b>⌘↵</b> broadcast to all online
      </span>
    ),
    render: (ctx) => <FleetRoom ctx={ctx} onOpenAgent={opts.onOpenAgent} />,
  };
}
