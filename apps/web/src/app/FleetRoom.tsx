// The Fleet Room (⌘K, ADR 0042 + the palette-UX overhaul). A native palette morph-view
// that makes the fleet feel like a Discord room: a roster of presence-aware MEMBERS on the
// left, the live fleet activity feed on the right, a broadcast bar below. Click a member
// to DM it — that's the wired ⌘K chat (PaletteChat) pointed at the member
// (`ctx.enter("member-dm", …)`), streaming through the hub proxy, with Back to the roster.
// The bottom bar broadcasts to everyone online (the @everyone announce — the only
// fire-and-forget path, since you can't stream N replies into one pane).
//
// Entered from the palette's "Agents" group; the DS CommandPalette supplies the
// back/close chrome + footer. Ungated for now — it reflects whatever api.fleet() returns
// for this window; host-scoping (cf. fleetSettingsGate) can follow.
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
import { FleetActivityFeed, markMemberRunning, pushFleetEvent, useMemberRunning } from "./FleetActivity";
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

function FleetRoom({ ctx, onOpenAgent }: { ctx: PaletteContext; onOpenAgent: (slug: string) => void }) {
  const { data: fleet } = useQuery(fleetQuery());
  const qc = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState("");
  const [target, setTarget] = useState<"broadcast" | string>("broadcast");
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
  const onlineCount = roster.filter((a) => a.running).length;
  const running = useMemberRunning();

  // DM a member = the wired chat, retargeted. Push it on the palette stack so Back/Escape
  // return here. Only running members are reachable.
  const dm = (a: FleetAgent) => {
    if (!a.running) return;
    ctx.enter("member-dm", { slug: slugOf(a), name: a.name });
  };

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

  const broadcast = () => {
    const msg = draft.trim();
    if (!msg) return;
    if (!broadcastTargets.length) {
      toast({ tone: "error", title: "No one to broadcast to", message: "No other members are online." });
      return;
    }
    // Fire-and-forget fan-out — each member runs the turn durably on its own instance.
    // Mark each busy now (optimistic "running" pill); its terminal turn.usage clears it.
    for (const a of broadcastTargets) {
      markMemberRunning(slugOf(a));
      api
        .sendToAgent(slugOf(a), msg)
        .catch((e) => toast({ tone: "error", title: `Couldn't reach ${a.name}`, message: errMsg(e) }));
    }
    toast({
      tone: "success",
      title: `Broadcast to ${broadcastTargets.length} member${broadcastTargets.length > 1 ? "s" : ""}`,
      message: clip(msg),
    });
    pushFleetEvent({ source: "you", text: `broadcast to ${broadcastTargets.length}: “${clip(msg, 48)}”`, kind: "broadcast" });
    setDraft("");
    inputRef.current?.focus();
  };

  // @-mention in the composer: a trailing "@token" opens a member picker; picking sets the
  // address target (a chip) and strips the token. No target chip = broadcast to all online.
  const mention = (() => {
    const m = draft.match(/(?:^|\s)@([\w-]*)$/);
    return m ? m[1].toLowerCase() : null;
  })();
  const mentionMatches =
    mention !== null
      ? roster.filter((a) => slugOf(a) !== here && a.name.toLowerCase().includes(mention)).slice(0, 6)
      : [];
  const pickMention = (a: FleetAgent) => {
    setTarget(slugOf(a));
    setDraft((d) => d.replace(/(?:^|\s)@[\w-]*$/, ""));
    inputRef.current?.focus();
  };

  const targetAgent = target === "broadcast" ? undefined : roster.find((a) => slugOf(a) === target);

  // Send: an addressed member opens its DM with the message pre-sent (the wired chat streams
  // the reply); otherwise broadcast to all online. ⌘↵ always broadcasts.
  const submit = (forceBroadcast: boolean) => {
    const msg = draft.trim();
    if (!msg) return;
    if (!forceBroadcast && targetAgent) {
      if (!targetAgent.running) {
        toast({ tone: "error", title: `${targetAgent.name} is offline`, message: "Start it first, or broadcast." });
        return;
      }
      ctx.enter("member-dm", { slug: slugOf(targetAgent), name: targetAgent.name, initial: msg });
      return;
    }
    broadcast();
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (mentionMatches.length && (e.key === "Enter" || e.key === "Tab")) {
      e.preventDefault();
      pickMention(mentionMatches[0]);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      submit(e.metaKey || e.ctrlKey);
    }
  };

  return (
    <div className="flr">
      <div className="flr__cols">
        <div className="flr__col flr__roster">
          <div className="flr__colhead">
            <h2>Members</h2>
            <span className="flr__count">
              {onlineCount} online · {roster.length}
            </span>
          </div>
          <div className="flr__list" role="group" aria-label="Fleet members">
            {roster.length === 0 && (
              <div className="flr__empty">No members yet — add one from Settings ▸ Agents.</div>
            )}
            {roster.map((a) => {
              const slug = slugOf(a);
              const p = presenceOf(a);
              const local = !a.host && !a.remote; // only a local process can be started/stopped here
              return (
                <div key={slug} className={`flr__member${a.running ? "" : " is-down"}`}>
                  <span className={`flr__dot flr__dot--${p.key}`} aria-hidden />
                  <button
                    type="button"
                    className="flr__who"
                    onClick={() => dm(a)}
                    disabled={!a.running}
                    title={a.running ? `Message ${a.name}` : `${a.name} is offline`}
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
                    {running[slug] ? (
                      <span className="flr__pill flr__pill--run" title="A turn is in flight">
                        running
                      </span>
                    ) : null}
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
                      title={a.running ? "Open full console" : "Offline"}
                      aria-label={`Open ${a.name} console`}
                    >
                      <ExternalLink size={14} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="flr__col flr__activity">
          <FleetActivityFeed />
        </div>
      </div>

      <div className="flr__composer">
        {mentionMatches.length > 0 && (
          <div className="flr__mentions" role="listbox" aria-label="Address a member">
            {mentionMatches.map((a) => {
              const mp = presenceOf(a);
              return (
                <button
                  key={slugOf(a)}
                  type="button"
                  className="flr__mention"
                  onMouseDown={(e) => {
                    e.preventDefault(); // keep input focus; fire before blur
                    pickMention(a);
                  }}
                >
                  <span className={`flr__dot flr__dot--${mp.key}`} aria-hidden />
                  <span className="flr__mention-name">{a.name}</span>
                  <span className="flr__mention-meta">{mp.label}</span>
                </button>
              );
            })}
          </div>
        )}
        <button
          type="button"
          className={`flr__target${targetAgent ? "" : " is-cast"}`}
          onClick={() => setTarget("broadcast")}
          title={targetAgent ? `Messaging ${targetAgent.name} — click to broadcast instead` : "Broadcasting to all online members"}
        >
          {targetAgent ? (
            <>
              <span>@{targetAgent.name}</span>
              <span className="flr__target-x" aria-hidden>
                ×
              </span>
            </>
          ) : (
            <>
              <Radio size={13} />
              <span>All online · {broadcastTargets.length}</span>
            </>
          )}
        </button>
        <input
          ref={inputRef}
          className="flr__input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={targetAgent ? `Message @${targetAgent.name}…` : "Message everyone…  (@ to address one)"}
          aria-label={targetAgent ? `Message ${targetAgent.name}` : "Broadcast message"}
        />
        <button
          type="button"
          className="flr__send"
          onClick={() => submit(false)}
          disabled={!draft.trim() || (!targetAgent && broadcastTargets.length === 0)}
          aria-label={targetAgent ? `Message ${targetAgent.name}` : "Broadcast"}
        >
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
    width: 780,
    footerHint: (
      <span className="flr__hint">
        <span>
          <kbd className="flr__kbd">click</kbd> DM a member
        </span>
        <span>
          <kbd className="flr__kbd">@</kbd> address in composer
        </span>
        <span>
          <kbd className="flr__kbd">↵</kbd> send · <kbd className="flr__kbd">⌘↵</kbd> broadcast
        </span>
      </span>
    ),
    render: (ctx) => <FleetRoom ctx={ctx} onOpenAgent={opts.onOpenAgent} />,
  };
}
