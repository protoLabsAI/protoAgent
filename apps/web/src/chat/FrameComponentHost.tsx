import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";

import { consoleTheme, INIT_REPOST_DELAYS } from "../app/pluginFrameHandshake";
import { InlineFrameBridgeContext } from "../artifacts/ArtifactRefChip";
import { createFrameRegistry, useLazyMount, type FrameRegistry } from "../artifacts/inlineFrames";
import { apiUrl } from "../lib/api";

// Host for a FRAME-rendered plugin component (ADR 0118 D5 console, S12). A plugin kind may
// declare a `frame` — a plugin-served page (`/plugins/<id>/<frame>`, from GET /api/components)
// that renders the component instead of a core widget. We mount that page in a maximally
// locked-down iframe and hand it the component payload + the console theme over postMessage.
//
// Trust model — this is the crux of the slice, so it is deliberate:
//   • The iframe is `sandbox="allow-scripts"` with NO `allow-same-origin`. Even though the
//     frame page is served from the console's OWN origin, the sandbox forces it into a unique
//     OPAQUE origin: it cannot read the console's cookies/localStorage (where the operator
//     bearer lives), cannot reach same-origin APIs with the operator's credentials, and cannot
//     break out of the frame. Model-reachable component payloads therefore run fully fenced.
//   • We NEVER post the operator bearer to the frame (contrast PluginView's `protoagent:init`,
//     which hands trusted first-party views the token). The only messages this host sends are
//     the component props and the console theme — neither is a secret. So we reuse the theme
//     half of the handshake module (`consoleTheme` / the re-post schedule) but NONE of its
//     bearer path.
//   • Because the frame's origin is opaque, a host→frame post CANNOT name a concrete target
//     origin (an opaque origin never matches `/plugins/...`'s real origin, so the browser
//     would drop the message). We post with `"*"`, which is safe precisely because the payload
//     carries no secret — the whole point of withholding the bearer above.
//   • The frame loads the design-system kit from `/_ds/plugin-kit.{css,js}` for its styling.
//     That prefix is on the auth public allowlist (`/_ds/` in a2a_impl/auth.py `_is_public`,
//     mounted on every tier by server/__init__.py `mount_ds_plugin_kit`), so the opaque-origin
//     frame can fetch the kit WITHOUT a bearer — which it has to, since it holds no credentials
//     and a `<link>`/`import()` can't carry one anyway.
//
// Cost bounds are the shared inline-frame budget (inlineFrames.ts, ADR 0118 D2): the reported
// content height is clamped to [80, 1200] px, the frame lazy-mounts only as it nears the
// viewport, and a per-chat-view registry keeps at most six frames live at once — a seventh
// evicts the least-recently-visible one, which falls back to a static card at its last height.
//
// Resolution order (which host renders a given component name) lives in ChatComponent + the
// componentRegistry resolver (S12b); this host takes a resolved `frameUrl` and renders it, and
// owns the send/openLink bridge below.
//
// Send-to-chat / openLink bridge (ADR 0118 D4 / S12b) — the second crux of this slice:
//   • The frame calls `window.protoComponent.send(text)` / `.openLink(url)` (the plugin-kit
//     shim, served with the frame). The shim relays the request UP to this host as a
//     `protoComponent:send|openLink` message carrying a correlation id (`cid`); the host runs it
//     through the SAME frameBridge gates artifacts use (reused via InlineFrameBridgeContext) and
//     posts the verdict back down as `protoComponent:bridgeResult`.
//   • The TRUST decision is the host's, never the model/plugin-authored frame: the gates check
//     the HOST's own `navigator.userActivation` (User Activation v2 propagates a child frame's
//     gesture to its ancestors, so a frame can't fake it by posting on its own), the 1-per-2s
//     rate window, the content length, and whether the target chat is busy.
//   • An accepted send goes through the EXACT send path S10b wired (ComponentChatSend.send →
//     ChatSessionSlot runTurn), tagged with the origin `{via:"component", kind, plugin}` so the
//     turn is a normal, visible, auditable user message with a "from ‹title›" label.

// The per-chat-view registry every frame-rendered plugin component shares, so the
// six-live-frame cap (inlineFrames.ts, ADR 0118 D2) bounds them TOGETHER — a seventh live
// component evicts the least-recently-visible one to a static card. ChatComponent (the one
// production caller) reads this and hands it to each FrameComponentHost; ChatSessionSlot scopes
// a fresh one PER CHAT VIEW via the Provider, so one tab's components can't evict another's. A
// single module default keeps the budget bounded even for a standalone render (a test, a future
// surface) that doesn't wrap a Provider — mirrors InlineFrameHostContext for artifacts. Without
// this, each host fell back to its OWN one-frame registry and the cap was never enforced.
const sharedComponentFrameRegistry = createFrameRegistry();
export const ComponentFrameRegistryContext = createContext<FrameRegistry>(sharedComponentFrameRegistry);

export type FrameComponentHostProps = {
  /** Stable id for this component OCCURRENCE — the frame-budget registry key and the
   *  `touch`/`measure`/`release` handle, AND the per-frame rate-limit key for the send gate.
   *  It must be unique PER OCCURRENCE, never the component kind: the registry reference-counts a
   *  repeated id into one shared slot, so passing the kind would collapse every render of that
   *  kind in a transcript into a single slot (defeating the six-live-frame cap) and make them
   *  share one remembered height and one send rate window. ChatComponent mints one per occurrence
   *  with `useId()`. Two deliberate mounts that DO pass the same id (e.g. a handover twin) share
   *  one slot by design — see the reference-count note in inlineFrames.register. */
  id: string;
  /** The resolved `/plugins/<id>/<frame>` page this component renders in (from the catalog's
   *  `frame_url`). Routed through `apiUrl` for the slug-aware base, like every plugin frame. */
  frameUrl: string;
  /** The component payload handed to the frame on init. Forwarded verbatim; the host adds
   *  NOTHING to it — in particular never the operator bearer. */
  props: Record<string, unknown>;
  /** The component-v1 kind this frame renders. Stamped onto a send's origin tag
   *  (`{via:"component", kind, plugin}`) so a frame-started turn stays auditable. */
  kind?: string;
  /** The plugin that owns the frame, for the same origin tag (null for a core frame kind). */
  plugin?: string | null;
  /** The per-chat-view frame registry enforcing the six-live-frame cap. Omit for a standalone
   *  host (it gets its own registry, so the cap is trivially satisfied). */
  registry?: FrameRegistry;
  /** Accessible title for the iframe. */
  title?: string;
};

/** How a frame-rendered plugin component reaches the chat it is shown in (ADR 0118 D4 / S12b).
 *  The hosting ChatSessionSlot provides it (ComponentChatSendContext.Provider); `send` runs the
 *  text through the NORMAL send path as a visible, origin-tagged user turn (ChatMessage.sentVia),
 *  and `isBusy` answers the gate's busy check for THIS session. Absent (no provider — a test, or a
 *  standalone render) ⇒ the host refuses the send so the frame's Promise rejects instead of
 *  hanging. Mirrors ArtifactChatSend, with a component-shaped origin. */
export type ComponentChatSend = {
  sessionId: string;
  /** True when THIS session already has a turn running / parked — the D4 "the agent is busy" gate. */
  isBusy: () => boolean;
  /** Post `text` as a visible user turn tagged with the component it came from. */
  send: (text: string, origin: { kind: string; plugin: string | null; title?: string }) => void;
};
export const ComponentChatSendContext = createContext<ComponentChatSend | null>(null);

/** The host's inline verdict on a send/openLink (ADR 0118 D4): the needs-confirm prompt (the
 *  runtime lacks the User Activation API, so the host asks before posting) or a rejection the
 *  operator sees in place ("the agent is busy", a bad gesture, rate-limited, a refused link). */
type BridgeNotice =
  | { kind: "confirm"; cid: number; text: string; message: string }
  | { kind: "rejected"; message: string };

// A per-registry subscriber set so every host sharing a registry re-renders when the live set
// changes — a sibling registering past the cap evicts one of them, and the evicted host must
// drop its live iframe for the static card. Keyed by the registry instance (WeakMap) so it is
// scoped to the one chat view that owns the registry and is GC'd with it.
const budgetSubscribers = new WeakMap<FrameRegistry, Set<() => void>>();

function budgetSet(reg: FrameRegistry): Set<() => void> {
  let set = budgetSubscribers.get(reg);
  if (!set) {
    set = new Set();
    budgetSubscribers.set(reg, set);
  }
  return set;
}

function notifyBudget(reg: FrameRegistry): void {
  for (const fn of [...budgetSet(reg)]) fn();
}

// Post a message to the (opaque-origin) frame. Target `"*"` is required — see the trust note
// above — and safe because nothing secret is ever passed here. Swallows the cross-origin /
// detached-window throw: best effort, exactly like the handshake module's posts.
function postToFrame(win: Window | null | undefined, message: Record<string, unknown>): void {
  if (!win) return;
  try {
    win.postMessage(message, "*");
  } catch {
    /* detached / cross-origin — best effort */
  }
}

/** Claim a live slot in the shared budget while `want` holds. Registers on the way in (which
 *  may evict the least-recently-visible sibling), releases on the way out, and re-renders this
 *  host whenever ANY host sharing the registry changes the live set — so an evicted host flips
 *  to its static card. Returns whether this host currently holds a live slot. */
function useFrameSlot(registry: FrameRegistry, id: string, want: boolean): boolean {
  const [, bump] = useReducer((n: number) => n + 1, 0);
  // Register in a layout effect (before paint) so the first painted frame already reflects the
  // slot decision — no flash of a static card before the live iframe, or vice versa. Subscribe
  // BEFORE registering so this host's own `notifyBudget` re-renders it into its now-live state,
  // and so any sibling evicted by this registration re-renders into its static card.
  useLayoutEffect(() => {
    if (!want) return;
    const set = budgetSet(registry);
    set.add(bump);
    registry.register(id); // may evict the least-recently-visible sibling
    notifyBudget(registry);
    return () => {
      set.delete(bump);
      registry.release(id);
      notifyBudget(registry);
    };
  }, [registry, id, want]);
  return want && registry.isLive(id);
}

export function FrameComponentHost({ id, frameUrl, props, kind, plugin, registry, title }: FrameComponentHostProps) {
  // A standalone host (no shared registry) gets its own, so lazy-mount + clamp still apply and
  // the single frame is always within the cap.
  const [ownRegistry] = useState(() => registry ?? createFrameRegistry());
  const reg = registry ?? ownRegistry;

  // How THIS component frame reaches its chat (D4) — null when no provider wraps the transcript
  // (a test, or a standalone render), in which case a send is refused. The host-side gate bridge
  // is reused from the artifact inline frames (InlineFrameBridgeContext) so the 1-per-2s window
  // is consistent across the whole transcript; the gate DECISIONS live in frameBridge, never in
  // the model/plugin-authored frame.
  const chat = useContext(ComponentChatSendContext);
  const bridge = useContext(InlineFrameBridgeContext);
  // The host's inline verdict (needs-confirm prompt or a rejection); cleared when the operator
  // answers or a fresh request arrives.
  const [bridgeNotice, setBridgeNotice] = useState<BridgeNotice | null>(null);

  // Near-viewport lazy mount: the placeholder carries the observer ref; `mounted` sticks true
  // once the frame has scrolled within range.
  const { ref: placeholderRef, mounted } = useLazyMount<HTMLDivElement>();
  const live = useFrameSlot(reg, id, mounted);

  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const loadedRef = useRef(false);
  const initTimers = useRef<number[]>([]);
  // Latest props/url read at post time (not captured at mount), so a re-post always carries
  // the current payload.
  const propsRef = useRef(props);
  propsRef.current = props;

  // Start at this id's last remembered height (floor if it never measured), so a resumed frame
  // and the static card both keep their size and the transcript layout doesn't jump.
  const [height, setHeight] = useState(() => reg.heightOf(id));

  const src = apiUrl(frameUrl);

  // The frame reports its content height; clamp it through the shared budget and size to it.
  // The frame is opaque-origin, so `e.origin` is "null" and useless for trust — we gate on the
  // source window identity instead, which is exactly this frame's `contentWindow`.
  useEffect(() => {
    if (!live) return;
    function onMessage(e: MessageEvent) {
      if (!frameRef.current || e.source !== frameRef.current.contentWindow) return;
      const data = e.data as { type?: unknown; height?: unknown } | null;
      if (data?.type === "protoComponent:height" && typeof data.height === "number") {
        setHeight(reg.measure(id, data.height));
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [live, reg, id]);

  // Live re-theme (ADR 0026/0042): the console fires a `protoagent:theme` window event on any
  // theme/accent change. Re-post the FRESH theme (read at fire time) to the frame so it
  // repaints without a reload — same contract as usePluginFrameThemeSync, but posted to the
  // opaque origin (`"*"`) and carrying NO bearer.
  useEffect(() => {
    if (!live) return;
    function onThemeChange() {
      if (!loadedRef.current) return;
      postToFrame(frameRef.current?.contentWindow, { type: "protoComponent:theme", theme: consoleTheme() });
    }
    window.addEventListener("protoagent:theme", onThemeChange);
    return () => window.removeEventListener("protoagent:theme", onThemeChange);
  }, [live]);

  // Post the host's verdict back down to THIS frame (the plugin-kit shim correlates it by `cid`
  // and settles the frame's send()/openLink() Promise). Always to our own frame's window, target
  // "*" (the frame is opaque-origin) — safe because the verdict carries no secret.
  const replyToFrame = useCallback((cid: number, result: { ok: boolean; text?: string; error?: string }) => {
    postToFrame(frameRef.current?.contentWindow, { type: "protoComponent:bridgeResult", cid, ...result });
  }, []);

  // The send/openLink bridge (ADR 0118 D4 / S12b). Gated on `e.source` being THIS frame's own
  // window — a sibling frame or the page can't drive another component's chat — the strong
  // guarantee, since an opaque-origin frame's `origin` is "null". Only live frames carry a window
  // to match. Every request runs through the shared frameBridge gates; the verdict goes back down
  // via replyToFrame and is shown to the operator in place.
  useEffect(() => {
    if (!live) return;
    function onBridge(e: MessageEvent) {
      const win = frameRef.current?.contentWindow;
      if (!win || e.source !== win) return; // only our own frame
      const m = (e.data || {}) as { type?: unknown; cid?: unknown; text?: unknown; url?: unknown };
      if (m.type !== "protoComponent:send" && m.type !== "protoComponent:openLink") return;
      const cid = typeof m.cid === "number" ? m.cid : NaN;
      if (Number.isNaN(cid)) return;

      if (m.type === "protoComponent:openLink") {
        // https-only, narrowed by the operator's optional origin allowlist (wired in a later
        // slice); on accept the HOST opens the tab with the mandatory noopener,noreferrer.
        const verdict = bridge.checkOpenLink({ url: String(m.url ?? "") });
        if (verdict.status === "ok") {
          window.open(verdict.url, "_blank", "noopener,noreferrer");
          setBridgeNotice(null);
          replyToFrame(cid, { ok: true });
        } else {
          setBridgeNotice({ kind: "rejected", message: verdict.message });
          replyToFrame(cid, { ok: false, error: verdict.message });
        }
        return;
      }

      // send: a visible user turn into THIS chat. Refuse up front when no chat is wired.
      if (!chat) {
        const message = "This component can't send to chat here.";
        setBridgeNotice({ kind: "rejected", message });
        replyToFrame(cid, { ok: false, error: message });
        return;
      }
      // The host checks its OWN user activation (User Activation v2 propagates a child frame's
      // gesture to its ancestors, so a frame can't fake it by posting on its own). A runtime
      // missing the API yields needs-confirm, not a silent trust.
      const ua = (navigator as Navigator & { userActivation?: { isActive: boolean } }).userActivation;
      const verdict = bridge.checkSend({ frameId: id, text: String(m.text ?? ""), userActivation: ua, isBusy: chat.isBusy });
      const origin = { kind: kind ?? "", plugin: plugin ?? null };
      if (verdict.status === "ok") {
        setBridgeNotice(null);
        chat.send(verdict.text, origin);
        replyToFrame(cid, { ok: true, text: verdict.text });
      } else if (verdict.status === "rejected") {
        setBridgeNotice({ kind: "rejected", message: verdict.message });
        replyToFrame(cid, { ok: false, error: verdict.message });
      } else {
        // needs-confirm: ask the operator inline, resolve the frame's Promise once they answer.
        setBridgeNotice({ kind: "confirm", cid, text: verdict.text, message: verdict.message });
      }
    }
    window.addEventListener("message", onBridge);
    return () => window.removeEventListener("message", onBridge);
  }, [live, id, kind, plugin, chat, bridge, replyToFrame]);

  // Answer the needs-confirm prompt (reads this render's notice, so it's always the live one). On
  // "send" the request goes out now, so advance the frame's rate window (checkSend never did — it
  // returned needs-confirm) and post the user turn; either way settle the frame's Promise so
  // send() doesn't hang to its timeout.
  function resolveConfirm(accept: boolean) {
    if (!bridgeNotice || bridgeNotice.kind !== "confirm") return;
    if (accept && chat) {
      bridge.noteSend(id);
      chat.send(bridgeNotice.text, { kind: kind ?? "", plugin: plugin ?? null });
      replyToFrame(bridgeNotice.cid, { ok: true, text: bridgeNotice.text });
    } else {
      replyToFrame(bridgeNotice.cid, { ok: false, error: "Send cancelled." });
    }
    setBridgeNotice(null);
  }

  // Clear any pending init re-posts when the frame unmounts / re-points.
  useEffect(() => {
    return () => {
      initTimers.current.forEach((t) => clearTimeout(t));
      initTimers.current = [];
      loadedRef.current = false;
    };
  }, [live, src]);

  function postInit(win: Window) {
    postToFrame(win, { type: "protoComponent:init", props: propsRef.current, theme: consoleTheme() });
  }

  function handleLoad(e: React.SyntheticEvent<HTMLIFrameElement>) {
    loadedRef.current = true;
    const win = e.currentTarget.contentWindow;
    if (!win) return;
    // The frame registers its `message` listener asynchronously (it dynamically imports the
    // plugin-kit), so the first init post can land before it is listening and be dropped. Post
    // once now, then re-post on the handshake module's short schedule; a retry lands once the
    // kit is ready, and init is idempotent on the kit side so the extra posts are harmless.
    initTimers.current.forEach((t) => clearTimeout(t));
    postInit(win);
    initTimers.current = INIT_REPOST_DELAYS.map((ms) =>
      window.setTimeout(() => {
        const w = frameRef.current?.contentWindow;
        if (w) postInit(w);
      }, ms),
    );
  }

  return (
    <div
      ref={placeholderRef}
      className="frame-component-host"
      data-testid="frame-component-host"
      style={{ height, minHeight: height }}
      data-frame-id={id}
    >
      {live ? (
        <iframe
          ref={frameRef}
          className="frame-component-host__frame"
          src={src}
          title={title ?? "Plugin component"}
          // allow-scripts ONLY — deliberately NO allow-same-origin (see the trust note above),
          // so the frame runs on an opaque origin with no access to the console's credentials.
          sandbox="allow-scripts"
          onLoad={handleLoad}
          style={{ width: "100%", height: "100%", border: 0 }}
        />
      ) : mounted ? (
        // Evicted to stay within the six-live-frame cap: a static card at the frame's last
        // measured height, so the transcript layout holds. (Resume interaction is S12b.)
        <div className="frame-component-host__evicted" role="status">
          Paused to save resources
        </div>
      ) : null}
      {/* The host's send/openLink verdict (ADR 0118 D4): the needs-confirm prompt, or a rejection
          shown in place. The frame also learns the verdict on its send()/openLink() Promise; this
          makes the decision visible to the operator, where their click landed. */}
      {bridgeNotice?.kind === "confirm" ? (
        <div className="frame-component-host__bridge" data-testid="component-send-confirm" role="alertdialog">
          <span className="frame-component-host__bridge-msg">{bridgeNotice.message}</span>
          <button
            type="button"
            className="frame-component-host__bridge-btn"
            data-testid="component-send-confirm-ok"
            onClick={() => resolveConfirm(true)}
          >
            Send
          </button>
          <button
            type="button"
            className="frame-component-host__bridge-btn"
            data-testid="component-send-confirm-cancel"
            onClick={() => resolveConfirm(false)}
          >
            Cancel
          </button>
        </div>
      ) : bridgeNotice?.kind === "rejected" ? (
        <div
          className="frame-component-host__bridge frame-component-host__bridge--rejected"
          data-testid="component-send-rejected"
          role="status"
        >
          <span className="frame-component-host__bridge-msg">{bridgeNotice.message}</span>
          <button
            type="button"
            className="frame-component-host__bridge-btn"
            data-testid="component-send-rejected-dismiss"
            aria-label="Dismiss"
            onClick={() => setBridgeNotice(null)}
          >
            Dismiss
          </button>
        </div>
      ) : null}
    </div>
  );
}
