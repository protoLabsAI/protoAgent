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
  const id = artifact.id;
  const src = useMemo(() => embedSrc(artifact), [artifact.id, artifact.version]);
  const { ref: slotRef, mounted } = useLazyMount<HTMLDivElement>();
  const live = useSyncExternalStore(host.subscribe, () => host.isLive(id));
  // Seeded from the optional `height` prop hint (0 = none), then driven by the frame's reported
  // height. Kept across a live→resume flip (the host component stays mounted) so the card
  // doesn't jump; the registry also remembers it for a remount.
  const [measured, setMeasured] = useState<number | null>(artifact.height || null);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const navigatedRef = useRef(false);
  const initTimers = useRef<number[]>([]);

  // Claim a live slot once the frame mounts (lazily); free it on unmount. Registering past the
  // cap evicts the least-recently-visible frame — its host re-reads `isLive` and renders the
  // resume card. A visibility observer keeps `touch` current so a frame scrolled away long ago
  // is the one that goes (not whichever mounted first).
  useEffect(() => {
    if (!mounted) return;
    host.register(id);
    const el = slotRef.current;
    let io: IntersectionObserver | undefined;
    if (el && typeof IntersectionObserver !== "undefined") {
      io = new IntersectionObserver(
        (entries) => {
          if (entries.some((e) => e.isIntersecting)) host.touch(id);
        },
        { rootMargin: "0px" },
      );
      io.observe(el);
    }
    return () => {
      io?.disconnect();
      host.release(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- slotRef is a stable ref object
  }, [mounted, host, id]);

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
      setMeasured(host.measure(id, h));
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [live, host, id, src]);

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

  const resume = useCallback(() => host.register(id), [host, id]);
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
