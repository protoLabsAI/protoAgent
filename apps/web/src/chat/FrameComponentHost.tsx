import { useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";

import { consoleTheme, INIT_REPOST_DELAYS } from "../app/pluginFrameHandshake";
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
// Resolution order (which host renders a given component name) and the send/openLink message
// bridge are S12b; this host takes a resolved `frameUrl` and renders it.

export type FrameComponentHostProps = {
  /** Stable id for this component instance — the frame-budget registry key and the
   *  `touch`/`measure`/`release` handle. Two mounts of the same component share one id. */
  id: string;
  /** The resolved `/plugins/<id>/<frame>` page this component renders in (from the catalog's
   *  `frame_url`). Routed through `apiUrl` for the slug-aware base, like every plugin frame. */
  frameUrl: string;
  /** The component payload handed to the frame on init. Forwarded verbatim; the host adds
   *  NOTHING to it — in particular never the operator bearer. */
  props: Record<string, unknown>;
  /** The per-chat-view frame registry enforcing the six-live-frame cap. Omit for a standalone
   *  host (it gets its own registry, so the cap is trivially satisfied). */
  registry?: FrameRegistry;
  /** Accessible title for the iframe. */
  title?: string;
};

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

export function FrameComponentHost({ id, frameUrl, props, registry, title }: FrameComponentHostProps) {
  // A standalone host (no shared registry) gets its own, so lazy-mount + clamp still apply and
  // the single frame is always within the cap.
  const [ownRegistry] = useState(() => registry ?? createFrameRegistry());
  const reg = registry ?? ownRegistry;

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
    </div>
  );
}
