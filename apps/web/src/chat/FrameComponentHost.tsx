import { snapshotToCss, themeTokenSnapshot } from "@protolabsai/design/embed";
import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from "react";

import { InlineFrameBridgeContext } from "../artifacts/ArtifactRefChip";
import { BridgeNoticeView, useFrameProposalGate } from "../artifacts/FrameProposalChip";
import { useFrameFocusGuard } from "../artifacts/frameFocusGuard";
import { FrameActivationShield, FrameQuarantineNotice } from "../artifacts/FrameGuardViews";
import { createFrameRegistry, LAZY_MOUNT_ROOT_MARGIN, useLazyMount, type FrameRegistry } from "../artifacts/inlineFrames";
import { apiUrl } from "../lib/api";
import { fetchComponentRead, type ComponentReadResult } from "../lib/api/components";
import { checkReadRequest, createReadLimiter } from "./frameComponentReads";

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
//     the component props and the console theme — neither is a secret. The theme is built by the
//     design system's embed layer (`themeTokenSnapshot` + `snapshotToCss`, ADR 0118 D5): the live
//     computed `--pl-*` snapshot, routed through the DS frame-injection guard before it crosses
//     into the frame. NONE of the plugin-view handshake's bearer path is reused, and init is not
//     re-posted on a schedule: it is pulled, once, by the frame's `protoComponent:ready` (below).
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
//   • The TRUST decision is the host's, never the model/plugin-authored frame. A send is a
//     PROPOSAL (S19 / #4137): it shows an inline confirm chip in console DOM and posts only when
//     the operator clicks Send — unless they ticked "Always allow from this component" (keyed by
//     plugin + kind, per browser). Either way the content length, busy, 1-per-2s send rate and
//     proposal rate gates apply. Shared with the artifact host via FrameProposalChip.
//   • An accepted send goes through the EXACT send path S10b wired (ComponentChatSend.send →
//     ChatSessionSlot runTurn), tagged with the origin `{via:"component", kind, plugin}` so the
//     turn is a normal, visible, auditable user message with a "from ‹title›" label.
//
// Read channel (ADR 0118 D5 amendment, #4143) — the one narrow way a frame gets LIVE data:
//   • The kind declares `reads` at registration (GET patterns under its OWN
//     `/api/plugins/<id>/`, validated server-side, carried by GET /api/components). The frame
//     posts `protoComponent:read {cid, path}` with `path` relative to that prefix.
//   • The host checks it all itself (frameComponentReads.ts): the message came from THIS frame's
//     window, the path matches a declared read (`*` = exactly one segment), GET only, ≤ 4
//     reads/s and ≤ 2 in flight per frame, and the frame is mounted + visible (servicing pauses
//     otherwise, which also keeps refresh polling cheap).
//   • It then makes the authed GET itself (fetchComponentRead: the operator's auth, slug-routed,
//     no redirects, JSON only, ≤ 256 KB) and posts `protoComponent:readResult {cid, ok, status,
//     json | error}`. The bearer, the response headers and any non-JSON body never reach the
//     frame. `status` is 0 when the host refused before any request was made.
//   • Reads only: writes and actions stay on the `send` bridge above, so every mutation is a
//     visible, confirmed user turn.
//
// Frame capability — a pulled, one-shot handshake (#4143 review F1 + re-review). `e.source ===
// contentWindow` alone is NOT an identity: a WindowProxy keeps its identity across navigations, and
// `sandbox="allow-scripts"` does not stop a frame navigating itself (a rendered link the operator
// clicks, or `location.href = …`). A page that replaced the plugin page would otherwise inherit its
// reads (and its send/openLink, which a kind's "Always allow" makes silent). Timed init re-posts
// were not enough either: a navigation during parse fires only the NEW page's load, and a page that
// navigated inside the re-post window could catch a re-posted init and stall its own load forever.
// So the capability is bound to the DECLARED page by a pairing secret only that page can read:
//   • Per iframe mount the host mints `pair` (128 random bits, hex) and loads the frame at
//     `<frame_url>#pair=<pair>`. A fragment is never sent to a server, never carried in a
//     Referer, and is dropped by any new navigation — so a document that replaced the plugin page
//     cannot know it.
//   • The frame's script posts `protoComponent:ready {pair}` — the value it read from
//     `location.hash` — exactly ONCE. The host answers a ready from this frame's window whose `pair`
//     matches, on a mount that has not yet been initialised and has seen at most one `load`, with
//     exactly ONE `protoComponent:init {props, theme, nonce}`. `nonce` is a second, fresh 128-bit
//     value, and init is the ONLY message that ever carries it. There are no re-posts.
//   • Every `protoComponent:read`, `protoComponent:send` and `protoComponent:openLink` must echo
//     `nonce`; a missing / wrong one is refused with no console UI.
//   • Any sign of a second document revokes the mount at once (nonce → null, in-flight reads
//     aborted, frame unloaded for a "navigated away" notice whose Reload remounts at the declared
//     URL with a fresh pair): a SECOND `load` of the element, a SECOND `ready`, or a `ready` whose
//     `pair` doesn't match (or that arrives after a second load).
//   • Residual, by design: the declared page itself can still hand its own capability away (put
//     the pair in a URL it navigates to, or navigate in the instant between posting ready and
//     receiving init). That is the plugin's own code, which already holds the plugin's reads.

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
  /** The kind's declared read allowlist (catalog `reads`, #4143): GET patterns relative to
   *  `/api/plugins/<plugin>/`. Empty/absent ⇒ every `protoComponent:read` is refused. */
  reads?: readonly string[];
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

// Reconstruct a `{ name: value }` record from a `selector{a:1;b:2}` rule emitted by snapshotToCss.
// snapshotToCss is the SOLE frame-injection guard (it alone decides what survives); this only
// READS its output, so the exact same drop decisions apply — never a second hand-written filter.
// Safe to split naively: snapshotToCss never emits a value carrying `;` (such values are dropped),
// and a custom-property name never contains `:`, so the first `:` splits name from value.
function cssRuleToRecord(rule: string): Record<string, string> {
  const out: Record<string, string> = {};
  const open = rule.indexOf("{");
  const close = rule.lastIndexOf("}");
  if (open < 0 || close <= open) return out;
  for (const decl of rule.slice(open + 1, close).split(";")) {
    const i = decl.indexOf(":");
    if (i < 0) continue;
    const name = decl.slice(0, i).trim();
    const value = decl.slice(i + 1).trim();
    if (name && value) out[name] = value;
  }
  return out;
}

// The theme payload posted into the opaque-origin component frame (ADR 0118 D5). Built by the
// design system's embed layer, NOT a hand-written token list: `themeTokenSnapshot` reads the
// console's live computed `--pl-*` values (plus `color-scheme`) off the document root, and
// `snapshotToCss` is the frame-injection guard — it drops any value containing `<`, `{`, `}` or
// `;` and any non-`--pl-` name. The frame's plugin-kit shim applies a `{ name: value }` record
// (setting `--pl-*` custom properties on its own :root), so we hand back exactly the entries the
// guard kept. Read at POST time (not captured at mount) so a live theme/accent switch re-posts
// fresh values and the frame repaints without a reload. SSR-safe: no document ⇒ empty theme.
function frameTheme(): Record<string, string> {
  if (typeof document === "undefined") return {};
  const css = snapshotToCss(themeTokenSnapshot(document.documentElement), ":root");
  return cssRuleToRecord(css);
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

/** Whether `ref`'s element is near the viewport (same margin as the lazy mount) while `enabled`.
 *  Read through the returned ref at message time, so a scroll never re-binds listeners. Where
 *  IntersectionObserver is unavailable it stays true — nothing is ever stuck paused. */
function useNearViewport(ref: React.RefObject<Element | null>, enabled: boolean): React.MutableRefObject<boolean> {
  const visible = useRef(true);
  useEffect(() => {
    visible.current = true;
    const el = ref.current;
    if (!enabled || !el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(
      (entries) => {
        const last = entries[entries.length - 1];
        if (last) visible.current = last.isIntersecting;
      },
      { rootMargin: LAZY_MOUNT_ROOT_MARGIN },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [ref, enabled]);
  return visible;
}

/** A fresh 128-bit capability for one frame mount, as hex. */
function mintNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function documentHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function FrameComponentHost({ id, frameUrl, props, kind, plugin, registry, title, reads }: FrameComponentHostProps) {
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

  // Near-viewport lazy mount: the placeholder carries the observer ref; `mounted` sticks true
  // once the frame has scrolled within range.
  const { ref: placeholderRef, mounted } = useLazyMount<HTMLDivElement>();
  const live = useFrameSlot(reg, id, mounted);

  const frameRef = useRef<HTMLIFrameElement | null>(null);
  // The current mount's capability (see "Frame capability" above): `nonce` is set only by the
  // one init a correctly-paired `ready` earns, and revoked (null) by any sign of a second document.
  // `mountState` is reset whenever a NEW iframe element mounts (a fresh `pair`, below).
  const nonceRef = useRef<string | null>(null);
  const mountState = useRef<{ pair: string; loads: number; initSent: boolean }>({ pair: "", loads: 0, initSent: false });
  const [navigated, setNavigated] = useState(false);
  const [mountKey, setMountKey] = useState(0);
  // Latest props read at post time (not captured at mount), so the one init always carries
  // the current payload.
  const propsRef = useRef(props);
  propsRef.current = props;

  // The read allowlist + per-frame limiter (#4143). Reads are read through a ref at message time
  // so a catalog refresh never re-binds the listener; the limiter lives as long as this host.
  const readsRef = useRef(reads);
  readsRef.current = reads;
  const [readLimiter] = useState(() => createReadLimiter());
  const nearViewport = useNearViewport(placeholderRef, live);

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
      if (!nonceRef.current) return; // only a paired, initialised frame gets theme updates
      postToFrame(frameRef.current?.contentWindow, { type: "protoComponent:theme", theme: frameTheme() });
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

  // Does this message carry the CURRENT mount's capability? (False once revoked by navigation.)
  const hasCapability = useCallback((m: { nonce?: unknown }) => {
    const cap = nonceRef.current;
    return cap !== null && typeof m.nonce === "string" && m.nonce === cap;
  }, []);

  // The send gate + confirm chip (ADR 0118 D4 / S19, #4137) — the same one the artifact host uses.
  // Trust is per component KIND (plugin + kind, from the host's own props), not per occurrence.
  const gate = useFrameProposalGate({ bridge, frameId: id, trustKey: `component:${plugin ?? ""}:${kind ?? ""}`, reply: replyToFrame });

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
      const m = (e.data || {}) as { type?: unknown; cid?: unknown; text?: unknown; url?: unknown; nonce?: unknown };
      if (m.type !== "protoComponent:send" && m.type !== "protoComponent:openLink") return;
      const cid = typeof m.cid === "number" ? m.cid : NaN;
      if (Number.isNaN(cid)) return;
      // The per-mount capability (#4143 F1): a document that replaced the plugin page has none,
      // so it can neither propose a turn nor ride a kind's "Always allow". Refused WITHOUT any
      // console UI, so a navigated page can't spam the operator either.
      if (!hasCapability(m)) {
        replyToFrame(cid, { ok: false, error: "missing or invalid nonce" });
        return;
      }

      if (m.type === "protoComponent:openLink") {
        // A PROPOSAL like send (#4076 F1): https only, narrowed by the operator's
        // `artifact.open_link_origins`, rate-gated, then the "Open ‹host› in a new tab?" chip
        // unless this source's LINKS are trusted. The tab opens from the chip's click with
        // noopener/noreferrer, and the frame hears success only if it really opened.
        void gate.proposeOpen(cid, String(m.url ?? ""));
        return;
      }

      // send: a visible user turn into THIS chat. Refuse up front when no chat is wired.
      if (!chat) {
        const message = "This component can't send to chat here.";
        gate.showRejection(message);
        replyToFrame(cid, { ok: false, error: message });
        return;
      }
      // A PROPOSAL, never a post: the shared gate shows the confirm chip (untrusted) or posts a
      // trusted kind's text through the normal origin-tagged path, after the gates either way.
      const origin = { kind: kind ?? "", plugin: plugin ?? null };
      gate.propose(cid, String(m.text ?? ""), { isBusy: chat.isBusy, post: (text) => chat.send(text, origin) });
    }
    window.addEventListener("message", onBridge);
    return () => window.removeEventListener("message", onBridge);
  }, [live, id, kind, plugin, chat, bridge, replyToFrame, hasCapability, gate.propose, gate.proposeOpen, gate.showRejection]);

  // A self-focusing component must not pull the composer's focus into its frame (#4076): the
  // frame is click-to-activate (or Tab into it), and one that takes focus on its own is
  // quarantined — the same guard the inline artifact host uses (frameFocusGuard.ts).
  const guard = useFrameFocusGuard(frameRef, `component:${plugin ?? ""}:${kind ?? ""}:${id}`, live);

  // A fresh pairing secret per iframe MOUNT: every path that mounts a new element (lazy mount /
  // eviction resume via `live`, quarantine "Run it", a navigated-away Reload, a re-pointed src)
  // changes one of these deps, so a remount never reuses a pair that was already spent.
  const pair = useMemo(() => mintNonce(), [src, live, guard.quarantined, mountKey]);
  if (mountState.current.pair !== pair) {
    mountState.current = { pair, loads: 0, initSent: false };
    nonceRef.current = null;
  }
  const pairedSrc = `${src.split("#")[0]}#pair=${pair}`;

  // Revoke this mount: no read/send/openLink is honoured after this line, and the frame is unloaded.
  const revokeMount = useCallback(() => {
    nonceRef.current = null;
    mountState.current.initSent = true; // never init this mount again
    setNavigated(true);
  }, []);

  // The handshake (see "Frame capability"): ONE init per mount, pulled by a correctly-paired ready.
  // A layout effect, so the listener is in place before the frame's document can possibly run.
  useLayoutEffect(() => {
    if (!live || navigated) return;
    function onReady(e: MessageEvent) {
      const win = frameRef.current?.contentWindow;
      if (!win || e.source !== win) return;
      const d = (e.data || {}) as { type?: unknown; pair?: unknown };
      if (d.type !== "protoComponent:ready") return;
      const st = mountState.current;
      if (st.initSent || st.loads > 1 || typeof d.pair !== "string" || d.pair !== st.pair) {
        revokeMount(); // a second ready, a second document, or one that can't know the pair
        return;
      }
      st.initSent = true;
      const nonce = mintNonce();
      nonceRef.current = nonce;
      postInit(win, nonce);
    }
    window.addEventListener("message", onReady);
    return () => window.removeEventListener("message", onReady);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- postInit reads refs only
  }, [live, navigated, pair, revokeMount]);

  // The read channel (ADR 0118 D5 amendment, #4143). Same source gate as the bridge: only THIS
  // frame's own window, AND the current mount's nonce. Every check is the host's (checkReadRequest + the limiter + visibility);
  // the authed GET happens here and only `{cid, ok, status, json|error}` goes back down.
  // Servicing stops (and in-flight reads are ABORTED, via the cleanup) whenever the frame is not a
  // live, un-quarantined, un-navigated mount.
  const servingReads = live && !guard.quarantined && !navigated;
  useEffect(() => {
    if (!servingReads) return;
    const inflight = new Set<AbortController>();
    let alive = true;
    function reply(win: Window, cid: number, result: ComponentReadResult, cap?: string | null) {
      // Deliver only to the window that asked, only while it is still our live frame, and — for a
      // served read — only while the capability it was granted under is still current (a
      // navigation in between revokes it, and the answer is dropped).
      if (!alive || frameRef.current?.contentWindow !== win) return;
      if (cap !== undefined && nonceRef.current !== cap) return;
      postToFrame(win, { type: "protoComponent:readResult", cid, ...result });
    }
    function onRead(e: MessageEvent) {
      const win = frameRef.current?.contentWindow;
      if (!win || e.source !== win) return; // only our own frame
      const m = (e.data || {}) as { type?: unknown; cid?: unknown; path?: unknown; method?: unknown; nonce?: unknown };
      if (m.type !== "protoComponent:read") return;
      const cid = typeof m.cid === "number" ? m.cid : NaN;
      if (Number.isNaN(cid)) return;
      if (!hasCapability(m)) {
        reply(win, cid, { ok: false, status: 0, error: "missing or invalid nonce" });
        return;
      }
      const cap = nonceRef.current;
      if (documentHidden() || !nearViewport.current) {
        reply(win, cid, { ok: false, status: 0, error: "paused: the component is not visible" });
        return;
      }
      const check = checkReadRequest(m, plugin, readsRef.current);
      if (!check.ok) {
        reply(win, cid, { ok: false, status: 0, error: check.error });
        return;
      }
      const limited = readLimiter.acquire(Date.now());
      if (limited) {
        reply(win, cid, { ok: false, status: 0, error: limited });
        return;
      }
      const ctrl = new AbortController();
      inflight.add(ctrl);
      void fetchComponentRead(plugin as string, check.path, { signal: ctrl.signal })
        .then((result) => reply(win, cid, result, cap))
        .finally(() => {
          inflight.delete(ctrl);
          readLimiter.release();
        });
    }
    window.addEventListener("message", onRead);
    return () => {
      alive = false;
      window.removeEventListener("message", onRead);
      inflight.forEach((c) => c.abort()); // unmounted / evicted / quarantined / navigated: stop
    };
  }, [servingReads, plugin, readLimiter, nearViewport, hasCapability]);

  // Drop the capability when the frame unmounts / re-points.
  useEffect(() => {
    return () => {
      nonceRef.current = null; // the frame went away: its capability goes with it
    };
  }, [live, src]);

  // The init carries the mount's nonce — the ONLY message that ever does — and is posted once.
  function postInit(win: Window, nonce: string) {
    postToFrame(win, { type: "protoComponent:init", props: propsRef.current, theme: frameTheme(), nonce });
  }

  // Loads are counted per mount. The FIRST is the declared page; any later one means the frame
  // navigated, so the mount is revoked. Init is never posted from here — only on a paired ready.
  function handleLoad() {
    const st = mountState.current;
    st.loads += 1;
    if (st.loads > 1) revokeMount();
  }

  function reloadAfterNavigation() {
    setNavigated(false);
    setMountKey((k) => k + 1); // a NEW iframe element at the declared src → a fresh first load
  }

  return (
    <div
      ref={placeholderRef}
      className="frame-component-host"
      data-testid="frame-component-host"
      style={{ height, minHeight: height }}
      data-frame-id={id}
    >
      {live && navigated ? (
        // The frame navigated away from its plugin page (#4143 F1): unloaded, capability revoked.
        <div className="frame-component-host__navigated" role="status" data-testid="frame-component-navigated">
          This component navigated away from its page and was stopped.{" "}
          <button type="button" onClick={reloadAfterNavigation}>
            Reload
          </button>
        </div>
      ) : live && guard.quarantined ? (
        <FrameQuarantineNotice what="component" height={height} onRun={guard.run} />
      ) : live ? (
        <div className="frame-guard" data-active={guard.active ? "true" : undefined} style={{ height: "100%" }}>
          <iframe
            key={mountKey}
            ref={frameRef}
            className="frame-component-host__frame"
            src={pairedSrc}
            title={title ?? "Plugin component"}
            // allow-scripts ONLY — deliberately NO allow-same-origin (see the trust note above),
            // so the frame runs on an opaque origin with no access to the console's credentials.
            sandbox="allow-scripts"
            onLoad={handleLoad}
            style={{ width: "100%", height: "100%", border: 0 }}
          />
          <FrameActivationShield guard={guard} />
        </div>
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
      <BridgeNoticeView
        notice={gate.notice}
        testIdPrefix="component"
        source="component"
        onAccept={(alwaysAllow) =>
          gate.resolve(true, alwaysAllow, chat && {
            isBusy: chat.isBusy,
            post: (text) => chat.send(text, { kind: kind ?? "", plugin: plugin ?? null }),
          })
        }
        onDismiss={() => gate.resolve(false, false, null)}
        onClearRejection={gate.clear}
      />
    </div>
  );
}
