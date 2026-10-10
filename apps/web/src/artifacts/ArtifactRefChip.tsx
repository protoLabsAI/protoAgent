import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type SyntheticEvent,
} from "react";

import { postInit, scheduleInitReposts, usePluginFrameThemeSync } from "../app/pluginFrameHandshake";
import { api, apiUrl } from "../lib/api";
import { onTopic } from "../lib/events";
import {
  artifactRefFromProps,
  openArtifactRef,
  refName,
  refState,
  useArtifactViewAvailable,
  versionLabel,
  type ArtifactRef,
  type RefState,
} from "./artifactRef";
import { createFrameBridge, type FrameBridge } from "./frameBridge";
import {
  clampHeight,
  createFrameRegistry,
  MAX_LIVE_FRAMES,
  MIN_FRAME_HEIGHT,
  useLazyMount,
} from "./inlineFrames";

// The `artifact-ref` chat component (#3617): what the artifact plugin's create/revise tools
// leave in the transcript — `✨ <title> · v<n>` + the kind, click → the Artifact panel on
// exactly that artifact and version. A sibling of the code pane's `code-ref` chip (same
// classes, same inert fallback). The LIVE turn also auto-opens the panel (artifactRef.ts
// onLiveArtifactRef); this chip is the way back to a version later, and the only way in on a
// phone, where nothing opens by itself (ADR 0086).
//
// It asks the plugin's /refs route what exists NOW, so an older version reads "v2 of 5" and
// a deleted/evicted artifact renders inert instead of opening a panel that can't show it.
// Every string here renders as React text (title included — it's model-authored), never HTML.
//
// An INLINE ref (ADR 0118 D2 / S7) hosts the artifact's OWN frame right here in the transcript
// instead of the chip — see InlineArtifactHost below. A ref without `inline` is unchanged.

const REF_STALE_MS = 15_000;

// The artifact plugin's view page, served from its public namespace (ADR 0026); its chrome-less
// embed mode (S5) renders ONE version through the same frame builder as the panel.
const ARTIFACT_EMBED_PATH = "/plugins/artifact/view";
function embedSrc(ref: Pick<ArtifactRef, "id" | "version">): string {
  return `${ARTIFACT_EMBED_PATH}?embed=${encodeURIComponent(ref.id)}&v=${ref.version}`;
}

// The live-frame registry key for an inline ref: the artifact id AND its version, never the id
// alone. The backend re-emits an inline ref on every edit — update_artifact/rewrite_artifact keep
// the inline placement (plugins/artifact/_tools.py `ref_tail(art, inline=_store._is_inline(art))`)
// — so several version frames of one artifact coexist in the transcript. Keying by id alone would
// collapse them into one slot: the 6-frame cap wouldn't hold, and evicting or releasing one
// version would flip every sibling version with it. Version first (a digit-only run), then a
// space, then the id: the first space is always the separator, so distinct (id, version) pairs
// never alias even when the id itself contains spaces.
function frameKey(ref: Pick<ArtifactRef, "id" | "version">): string {
  return `v${ref.version} ${ref.id}`;
}

function useRefMeta(id: string, enabled: boolean) {
  const qc = useQueryClient();
  // A create/edit/delete anywhere (the agent, the panel's own editor or trash) → refresh the
  // chips that point at that artifact, so "v2 of 5" and "no longer available" stay true.
  useEffect(() => {
    if (!enabled) return;
    return onTopic("artifact.#", (data) => {
      const hit = (data as { id?: unknown } | null)?.id;
      if (hit === undefined || hit === id) void qc.invalidateQueries({ queryKey: ["artifact-ref", id] });
    });
  }, [enabled, id, qc]);
  return useQuery({
    queryKey: ["artifact-ref", id],
    queryFn: async () => (await api.artifactRefs([id])).artifacts[id] ?? null,
    enabled,
    staleTime: REF_STALE_MS,
    retry: 1,
  });
}

// ── inline frame host (ADR 0118 D2 / S7b) ──────────────────────────────────────────────────

/** A per-chat-view host governing the live inline frames: a {@link createFrameRegistry} (the
 *  pure S7a cap/height bookkeeping) made observable so an eviction re-renders the evicted
 *  frame's host into its "Click to resume" card. One host is shared by every inline chip in
 *  the view (via context), so the 6-frame cap bounds them together. */
export type InlineFrameHost = {
  /** Register `id` as live now; evicts the least-recently-visible frame past the cap, then
   *  notifies subscribers so the evicted host can swap to a resume card. */
  register(id: string): void;
  /** Mark `id` visible now (scrolled into view) so it isn't the next evicted. */
  touch(id: string): void;
  /** Record `id`'s reported content height; returns the clamped value. */
  measure(id: string, height: number): number;
  /** Drop `id` from the live set (its host unmounted); notifies if it had been live. */
  release(id: string): void;
  /** The last height remembered for `id` (the registry keeps it past eviction/release, so a
   *  remounted frame can restore its size before it remeasures); the floor if never measured. */
  heightOf(id: string): number;
  /** Is `id` a live frame right now? */
  isLive(id: string): boolean;
  /** Subscribe to live-set changes; returns an unsubscribe. */
  subscribe(listener: () => void): () => void;
};

export function createInlineFrameHost(cap: number = MAX_LIVE_FRAMES): InlineFrameHost {
  const reg = createFrameRegistry(cap);
  const listeners = new Set<() => void>();
  const emit = () => {
    for (const fn of listeners) fn();
  };
  return {
    register(id) {
      reg.register(id);
      emit(); // a register may evict another frame → let it re-render into a resume card
    },
    touch(id) {
      reg.touch(id);
    },
    measure(id, height) {
      return reg.measure(id, height);
    },
    release(id) {
      const was = reg.isLive(id);
      reg.release(id);
      if (was) emit();
    },
    heightOf: (id) => reg.heightOf(id),
    isLive: (id) => reg.isLive(id),
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

// The shared host for the console's inline frames. A single module default keeps the live-frame
// budget bounded across the transcript; a test (or a future per-chat-view wiring) can scope its
// own via the context Provider.
const sharedInlineFrameHost = createInlineFrameHost();
export const InlineFrameHostContext = createContext<InlineFrameHost>(sharedInlineFrameHost);

// ── send-to-chat / openLink host bridge (ADR 0118 D4 / S10b) ────────────────────────────────

/** How an inline artifact frame reaches the chat it is rendered in. The hosting ChatSessionSlot
 *  provides it (ArtifactChatSendContext.Provider); `send` runs the frame's text through the
 *  NORMAL send path as a visible, origin-tagged user turn (ChatMessage.sentVia), and `isBusy`
 *  answers the gate's busy check for THIS session. Absent (no provider — a test, or a standalone
 *  panel) ⇒ the host refuses the send, so the frame's Promise rejects instead of hanging. The
 *  panel can supply the same shape to reach the active chat tab. */
export type ArtifactChatSend = {
  sessionId: string;
  /** True when THIS session already has a turn running / parked — the D4 "the agent is busy" gate. */
  isBusy: () => boolean;
  /** Post `text` as a visible user turn tagged with where it came from. */
  send: (text: string, origin: { artifact_id: string; version: number; title?: string }) => void;
};
export const ArtifactChatSendContext = createContext<ArtifactChatSend | null>(null);

// One host-side bridge for the console's inline frames: it holds the per-frame send rate windows
// (frameBridge.ts). A single module default keeps the 1-per-2s gate consistent across the whole
// transcript; the gate DECISIONS live in frameBridge (D4), never in the model-authored frame. A
// test (or a future per-view wiring) can scope its own via the context Provider.
const sharedInlineFrameBridge = createFrameBridge();
export const InlineFrameBridgeContext = createContext<FrameBridge>(sharedInlineFrameBridge);

/** The host's verdict shown inline on an inline artifact: the D4 needs-confirm prompt (the
 *  runtime lacks the User Activation API, so the host asks before posting) or a rejection the
 *  operator sees in place ("the agent is busy", a bad gesture, rate-limited, a refused link). */
type BridgeNotice =
  | { kind: "confirm"; cid: number; text: string; message: string }
  | { kind: "rejected"; message: string };

function Inert({ name, version, reason, testId }: { name: string; version: string; reason: string; testId: string }) {
  return (
    <div className="code-ref-inert artifact-ref-inert" data-testid={testId}>
      <Sparkles size={13} aria-hidden className="code-ref-inert__icon" />
      <span>
        <span className="artifact-ref-chip__title">{name}</span>
        {` · ${version} — ${reason}`}
      </span>
    </div>
  );
}

// An inline ref: the chip's head (title · version · Open in panel) over the artifact's own embed
// frame (S5's /plugins/artifact/view?embed=<id>&v=<n>), hosted with the SAME bearer/theme
// handshake PluginView uses (S6) so there is no second frame builder or security model. The
// frame mounts lazily near the viewport (S7a), reports its content height (host-clamped to
// [80,1200] — taller content scrolls inside), and obeys the 6-frame cap: a frame evicted to make
// room becomes a static "Click to resume" card keeping its last height.
function InlineArtifactHost({
  artifact,
  name,
  label,
  older,
}: {
  artifact: ArtifactRef;
  name: string;
  label: string;
  older: boolean;
}) {
  const host = useContext(InlineFrameHostContext);
  // How THIS inline frame reaches its chat (D4 / S10b) — null when no provider wraps the
  // transcript (a test, or a standalone render), in which case a send is refused.
  const chat = useContext(ArtifactChatSendContext);
  // The host-side gate bridge (per-frame rate windows); the shared module default unless a
  // Provider scopes one (tests do, for isolation).
  const bridge = useContext(InlineFrameBridgeContext);
  // The host's inline verdict on a send/openLink: the needs-confirm prompt or a rejection the
  // operator sees in place. Cleared when the operator answers or a fresh request arrives.
  const [bridgeNotice, setBridgeNotice] = useState<BridgeNotice | null>(null);
  // Each (artifact, version) gets its OWN live slot — see frameKey: an inline artifact re-emits a
  // fresh ref per version, and keying by id alone would collapse the versions into one.
  const key = frameKey(artifact);
  const src = useMemo(() => embedSrc(artifact), [artifact.id, artifact.version]);
  const { ref: slotRef, mounted } = useLazyMount<HTMLDivElement>();
  // Whether THIS host currently holds its one live-slot claim on the registry (taken on mount,
  // dropped on unmount). An eviction drops our live slot but NOT this claim — inlineFrames
  // reference-counts holders — so this ref lets `resume` re-register without taking a second
  // claim (#4111), and lets the first render below treat the pre-register window as live.
  const claimedRef = useRef(false);
  const liveInStore = useSyncExternalStore(host.subscribe, () => host.isLive(key));
  // A just-mounted frame hasn't run its register effect yet, so the store still reads "not live"
  // for that first render and the resume card would flash before `live` settled true (#4111).
  // Treat the mounted-but-not-yet-claimed window as live; once claimed the store is authoritative
  // (claimedRef stays true across an eviction, so an evicted frame still falls to the resume card).
  const live = liveInStore || (mounted && !claimedRef.current);
  // Seeded from the registry's remembered height for this (id, version) so a remount restores the
  // frame's last size instead of snapping back to the hint or the floor (#4111); then driven by
  // the frame's reported height. heightOf returns the floor when nothing was ever measured, so a
  // first mount falls back to the optional `height` prop hint (0 = none). Kept across a
  // live→resume flip (the host component stays mounted) so the card doesn't jump.
  const [measured, setMeasured] = useState<number | null>(() => {
    const remembered = host.heightOf(key);
    return remembered > MIN_FRAME_HEIGHT ? remembered : artifact.height || null;
  });
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const navigatedRef = useRef(false);
  const initTimers = useRef<number[]>([]);

  // Bring this frame (back) live holding EXACTLY one claim. The eviction that drops our live slot
  // leaves our claim intact (inlineFrames reference-counts holders, and we are still mounted), so
  // registering outright would add a SECOND claim — then unmount's release() would see one claim
  // remaining and keep the key live with no frame, leaking the slot forever (#4111). Drop our
  // surviving claim first, then re-take exactly one. Used by the resume card's click AND by the
  // visibility observer below when an evicted frame scrolls back into view (#4123).
  const resume = useCallback(() => {
    if (claimedRef.current) host.release(key);
    host.register(key);
    claimedRef.current = true;
  }, [host, key]);

  // Claim a live slot once the frame mounts (lazily); free it on unmount. Registering past the
  // cap evicts the least-recently-visible frame — its host re-reads `isLive` and renders the
  // resume card. A visibility observer keeps `touch` current so a frame scrolled away long ago
  // is the one that goes (not whichever mounted first). And when an ALREADY-evicted frame scrolls
  // back into view — its slot taken by a frame in another chat tab that is now hidden but still
  // mounted (hidden tabs don't unmount, so they hold their slots) — it re-registers so
  // least-recently-seen eviction reclaims the slot from those hidden frames instead of leaving a
  // dead resume card (#4123). The re-register runs through `resume`, so it never double-claims —
  // the single-claim invariant (#4111) still holds.
  useEffect(() => {
    if (!mounted) return;
    host.register(key);
    claimedRef.current = true;
    const el = slotRef.current;
    let io: IntersectionObserver | undefined;
    if (el && typeof IntersectionObserver !== "undefined") {
      io = new IntersectionObserver(
        (entries) => {
          if (!entries.some((e) => e.isIntersecting)) return;
          if (host.isLive(key)) host.touch(key);
          else resume();
        },
        { rootMargin: "0px" },
      );
      io.observe(el);
    }
    return () => {
      io?.disconnect();
      host.release(key);
      claimedRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- slotRef is a stable ref; resume is stable per (host, key)
  }, [mounted, host, key, resume]);

  // Size the frame from the height it reports (protoArtifact:height — S5). Gated on e.source
  // being THIS frame's own window, the strong guarantee; the payload is a single int the shell
  // already clamped, so that source check is the whole gate (the shell itself notes a height is
  // low-stakes). Re-clamp host-side to [80,1200] — the host owns the contract — so taller
  // content scrolls inside the frame rather than growing the transcript. Also re-posts the
  // bearer/theme when the kit pings ready, exactly as PluginView does.
  useEffect(() => {
    if (!live) return;
    const onMessage = (e: MessageEvent) => {
      const win = frameRef.current?.contentWindow;
      if (!win || e.source !== win) return;
      const m = (e.data || {}) as { type?: unknown; height?: unknown };
      if (m.type === "protoagent:ready") {
        postInit(win, src);
        return;
      }
      if (m.type !== "protoArtifact:height") return;
      const h = typeof m.height === "number" ? m.height : NaN;
      setMeasured(host.measure(key, h));
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [live, host, key, src]);

  // Post the host's verdict back down to THIS embed frame (shell.js correlates it by `cid` and
  // relays it to the model frame's send()/openLink() Promise). Always to our own frame's window.
  const replyToFrame = useCallback((cid: number, result: { ok: boolean; text?: string; error?: string }) => {
    const win = frameRef.current?.contentWindow;
    if (!win) return;
    win.postMessage({ type: "protoArtifact:bridgeResult", cid, ...result }, "*");
  }, []);

  // The send/openLink bridge (ADR 0118 D4 / S10b). The shell relays an in-frame
  // protoArtifact.send()/openLink() UP to this host as `protoArtifact:send|openLink` carrying a
  // correlation id (`cid`) and the D4 origin metadata (`via/artifact_id/version`). The host owns
  // the TRUST decision — never the model-authored frame — so every request runs through the
  // frameBridge gates here, and the verdict goes back down via replyToFrame. Gated on e.source
  // being THIS frame's own window (a sibling or the nested frame can't drive another chip's chat),
  // which is the strong guarantee; only mounted/live frames carry a window to match.
  useEffect(() => {
    if (!live) return;
    const onBridge = (e: MessageEvent) => {
      const win = frameRef.current?.contentWindow;
      if (!win || e.source !== win) return; // only our own embed frame
      const m = (e.data || {}) as {
        type?: unknown;
        cid?: unknown;
        text?: unknown;
        url?: unknown;
        artifact_id?: unknown;
        version?: unknown;
      };
      if (m.type !== "protoArtifact:send" && m.type !== "protoArtifact:openLink") return;
      const cid = typeof m.cid === "number" ? m.cid : NaN;
      if (Number.isNaN(cid)) return;

      if (m.type === "protoArtifact:openLink") {
        // https-only, narrowed by the operator's optional origin allowlist (wired in a later
        // slice); on accept the HOST opens the tab with the mandatory noopener,noreferrer.
        const verdict = bridge.checkOpenLink({ url: String(m.url ?? "") });
        if (verdict.status === "ok") {
          window.open(verdict.url, "_blank", "noopener,noreferrer");
          replyToFrame(cid, { ok: true });
        } else {
          setBridgeNotice({ kind: "rejected", message: verdict.message });
          replyToFrame(cid, { ok: false, error: verdict.message });
        }
        return;
      }

      // send: a visible user turn into THIS chat. Refuse up front when no chat is wired.
      if (!chat) {
        const message = "This answer can't send to chat here.";
        setBridgeNotice({ kind: "rejected", message });
        replyToFrame(cid, { ok: false, error: message });
        return;
      }
      // The host checks its OWN user activation (User Activation v2 propagates a child frame's
      // gesture to its ancestors, so a frame can't fake it by posting on its own). A runtime
      // missing the API yields needs-confirm, not a silent trust. And the gesture must have landed
      // IN this frame — a click inside the iframe moves focus to it, so requiring
      // document.activeElement === our frame rejects a click on console chrome (a resume card, a
      // tab, "Open in panel") or a click meant for a sibling frame (#4122).
      const ua = (navigator as Navigator & { userActivation?: { isActive: boolean } }).userActivation;
      const verdict = bridge.checkSend({
        frameId: key,
        text: String(m.text ?? ""),
        userActivation: ua,
        isBusy: chat.isBusy,
        focusInFrame: () => document.activeElement === frameRef.current,
      });
      const origin = {
        artifact_id: typeof m.artifact_id === "string" ? m.artifact_id : artifact.id,
        version: typeof m.version === "number" ? m.version : artifact.version,
        title: name,
      };
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
    };
    window.addEventListener("message", onBridge);
    return () => window.removeEventListener("message", onBridge);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- frameRef/replyToFrame are stable; re-bind on live/key/chat
  }, [live, key, chat, artifact.id, artifact.version, name, replyToFrame]);

  // Answer the needs-confirm prompt (reads this render's notice, so it's always the live one). On
  // "send" the request goes out now, so advance the frame's rate window (checkSend never did — it
  // returned needs-confirm) and post the user turn; either way settle the frame's Promise so
  // send() doesn't hang to its timeout.
  function resolveConfirm(accept: boolean) {
    if (!bridgeNotice || bridgeNotice.kind !== "confirm") return;
    if (accept && chat) {
      bridge.noteSend(key);
      chat.send(bridgeNotice.text, { artifact_id: artifact.id, version: artifact.version, title: name });
      replyToFrame(bridgeNotice.cid, { ok: true, text: bridgeNotice.text });
    } else {
      replyToFrame(bridgeNotice.cid, { ok: false, error: "Send cancelled." });
    }
    setBridgeNotice(null);
  }

  // The live re-theme PluginView performs (ADR 0026 / S6), reused verbatim so the embed repaints
  // on a console theme switch without a reload.
  usePluginFrameThemeSync(frameRef, src, navigatedRef);

  // Hand the embed the bearer + theme after it navigates (never a token in the URL). The DS
  // plugin-kit registers its listener asynchronously, so re-post on a short schedule; the
  // ready-ping path above makes it exact when the kit announces itself.
  function handleLoad(e: SyntheticEvent<HTMLIFrameElement>) {
    navigatedRef.current = true;
    const win = e.currentTarget.contentWindow;
    if (!win) return;
    initTimers.current.forEach(clearTimeout);
    initTimers.current = scheduleInitReposts(win, src);
  }
  useEffect(
    () => () => {
      initTimers.current.forEach(clearTimeout);
      initTimers.current = [];
    },
    [],
  );

  const shownHeight = measured ?? MIN_FRAME_HEIGHT;

  return (
    <div
      ref={slotRef}
      className="artifact-ref-inline"
      data-testid="artifact-ref-inline"
      data-version={artifact.version}
      data-older={older ? "true" : undefined}
    >
      <div className="artifact-ref-inline__head">
        <Sparkles size={14} aria-hidden className="code-ref-chip__icon" />
        <span className="artifact-ref-chip__title">{name}</span>
        <span className="code-ref-chip__sep"> · </span>
        <span className="artifact-ref-chip__version">{label}</span>
        <span className="artifact-ref-inline__spacer" />
        <button
          type="button"
          className="artifact-ref-inline__open"
          data-testid="artifact-inline-open"
          title={`Open ${name} (${label}) in the Artifact panel`}
          onClick={() => openArtifactRef(artifact)}
        >
          Open in panel
        </button>
      </div>
      {!mounted ? (
        <div className="artifact-ref-inline__body" style={{ height: shownHeight }} />
      ) : live ? (
        // sandbox/allow mirror PluginView's plugin frame: same-origin so the DS kit + the gated
        // history poll work, pointer-lock delegated so a nested canvas/3D answer can capture the
        // mouse. The NESTED artifact frame the shell builds keeps its own stricter sandbox.
        <iframe
          ref={frameRef}
          className="artifact-ref-inline__frame"
          data-testid="artifact-inline-frame"
          src={apiUrl(src)}
          title={name}
          sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-pointer-lock"
          allow="clipboard-read; clipboard-write; pointer-lock"
          style={{ height: shownHeight }}
          onLoad={handleLoad}
        />
      ) : (
        <button
          type="button"
          className="artifact-ref-inline__resume"
          data-testid="artifact-inline-resume"
          style={{ height: shownHeight }}
          title={`Resume ${name}`}
          onClick={resume}
        >
          Click to resume
        </button>
      )}
      {/* The host's send/openLink verdict (ADR 0118 D4): the needs-confirm prompt, or a
          rejection shown in place. The frame also gets the verdict on its send()/openLink()
          Promise; this makes the decision visible to the operator, where their click landed. */}
      {bridgeNotice?.kind === "confirm" ? (
        <div className="artifact-ref-inline__bridge" data-testid="artifact-send-confirm" role="alertdialog">
          <span className="artifact-ref-inline__bridge-msg">{bridgeNotice.message}</span>
          <button
            type="button"
            className="artifact-ref-inline__bridge-btn"
            data-testid="artifact-send-confirm-ok"
            onClick={() => resolveConfirm(true)}
          >
            Send
          </button>
          <button
            type="button"
            className="artifact-ref-inline__bridge-btn"
            data-testid="artifact-send-confirm-cancel"
            onClick={() => resolveConfirm(false)}
          >
            Cancel
          </button>
        </div>
      ) : bridgeNotice?.kind === "rejected" ? (
        <div className="artifact-ref-inline__bridge artifact-ref-inline__bridge--rejected" data-testid="artifact-send-rejected" role="status">
          <span className="artifact-ref-inline__bridge-msg">{bridgeNotice.message}</span>
          <button
            type="button"
            className="artifact-ref-inline__bridge-btn"
            data-testid="artifact-send-rejected-dismiss"
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

export function ArtifactRefChip({ props }: { props: Record<string, unknown> }) {
  const available = useArtifactViewAvailable();
  const ref = artifactRefFromProps(props);
  const meta = useRefMeta(ref?.id ?? "", available && !!ref);
  if (!ref) return <div className="chat-comp chat-comp-unknown">[artifact-ref: missing artifact_id/version]</div>;
  const name = refName(ref);
  // A failed metadata read (an older plugin without /refs, a blip) must not strand the chip:
  // treat it as unknown and stay clickable — the panel itself handles a missing artifact.
  const state: RefState = meta.isSuccess ? refState(ref.version, meta.data) : { kind: "unknown" };
  const label = versionLabel(ref.version, state);
  if (!available) {
    return <Inert name={name} version={label} reason="the Artifact panel is off" testId="artifact-ref-off" />;
  }
  if (state.kind === "gone") {
    return <Inert name={name} version={label} reason="no longer available" testId="artifact-ref-gone" />;
  }
  if (state.kind === "trimmed") {
    return <Inert name={name} version={label} reason="this version is no longer kept" testId="artifact-ref-trimmed" />;
  }
  const older = state.kind === "older";
  // Inline placement (ADR 0118 D2): host the artifact's own embed frame here. History replay and
  // reattach reach this exact path, so an inline answer renders identically after a reload; a
  // ref WITHOUT `inline` falls through to the unchanged chip below.
  if (ref.inline) {
    return <InlineArtifactHost artifact={ref} name={name} label={label} older={older} />;
  }
  return (
    <button
      type="button"
      className="code-ref-chip artifact-ref-chip"
      data-testid="artifact-ref-chip"
      data-version={ref.version}
      data-older={older ? "true" : undefined}
      title={
        older
          ? `Open ${name} ${label} in the Artifact panel — an earlier version, so the panel stops following new ones`
          : `Open ${name} (${label}) in the Artifact panel`
      }
      onClick={() => openArtifactRef(ref)}
    >
      <Sparkles size={14} aria-hidden className="code-ref-chip__icon" />
      <span className="code-ref-chip__body">
        <span className="artifact-ref-chip__head">
          <span className="artifact-ref-chip__title">{name}</span>
          <span className="code-ref-chip__sep"> · </span>
          <span className="artifact-ref-chip__version">{label}</span>
          {ref.kind ? <span className="artifact-ref-chip__kind">{ref.kind}</span> : null}
        </span>
      </span>
    </button>
  );
}
