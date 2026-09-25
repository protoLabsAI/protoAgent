import { flushSync } from "react-dom";

import { openView } from "../app/palette/nav";
import { chatStore } from "../chat/chat-store";
import { postToPluginView } from "../lib/pluginViewInbox";
import { useUI } from "../state/uiStore";

// The `artifact-ref` chat component (#3617) — what the artifact plugin's create/revise tools
// emit: a POINTER to one artifact version, `{artifact_id, version, versions_total?, title,
// kind}` (plugins/artifact/_ref.py). `version` is the LIFETIME version number, the same one
// the panel's "v2 of 5" label counts. Props are untrusted data off the wire (a fork, an older
// server, a hand-written component-v1 part), so every field is checked, never cast.

export const ARTIFACT_REF_COMPONENT = "artifact-ref";
/** The artifact plugin's panel — `plugin:<id>:<view>` from its manifest. */
export const ARTIFACT_VIEW_KEY = "plugin:artifact:artifact";

const KINDS = new Set(["html", "svg", "mermaid", "react", "markdown", "file"]);

export type ArtifactRef = { id: string; version: number; title: string; kind: string };

export function artifactRefFromProps(props: Record<string, unknown> | undefined): ArtifactRef | null {
  if (!props) return null;
  const id = typeof props.artifact_id === "string" ? props.artifact_id.trim().slice(0, 64) : "";
  const v = props.version;
  const version = typeof v === "number" && Number.isInteger(v) && v >= 1 ? v : 0;
  if (!id || !version) return null;
  const title = typeof props.title === "string" ? props.title.replace(/\s+/g, " ").trim().slice(0, 200) : "";
  const kind = typeof props.kind === "string" && KINDS.has(props.kind) ? props.kind : "";
  return { id, version, title, kind };
}

/** The chip's name for an artifact: its title, else "<kind> artifact". */
export function refName(ref: Pick<ArtifactRef, "title" | "kind">): string {
  return ref.title || (ref.kind ? `${ref.kind} artifact` : "Artifact");
}

/** What the chip knows about the artifact NOW (from /refs), resolved against its version. */
export type RefState =
  | { kind: "unknown" } // not loaded yet, or the metadata route failed — still clickable
  | { kind: "latest"; total: number }
  | { kind: "older"; total: number }
  | { kind: "trimmed"; total: number } // this version was trimmed at the max_versions cap
  | { kind: "gone" }; // the artifact was deleted or evicted

export function refState(
  version: number,
  meta: { version_count: number; oldest: number } | null | undefined,
): RefState {
  if (meta === undefined) return { kind: "unknown" };
  if (meta === null) return { kind: "gone" };
  const total = Math.max(meta.version_count, version);
  if (version < meta.oldest) return { kind: "trimmed", total };
  return version >= total ? { kind: "latest", total } : { kind: "older", total };
}

/** `v2 of 5` for an older version, `v5` for the newest (or while unknown). */
export function versionLabel(version: number, state: RefState): string {
  return state.kind === "older" || state.kind === "trimmed" ? `v${version} of ${state.total}` : `v${version}`;
}

// ── opening the panel ────────────────────────────────────────────────────────────────

const MOBILE_QUERY = "(max-width: 767px)";

function isMobileViewport(): boolean {
  try {
    return typeof window !== "undefined" && window.matchMedia(MOBILE_QUERY).matches;
  } catch {
    return false;
  }
}

type Dock = "left" | "right" | "bottom" | "hidden";

function dockOf(id: string): Dock | null {
  const ro = useUI.getState().railOrder;
  if (ro.left.includes(id)) return "left";
  if (ro.right.includes(id)) return "right";
  if (ro.bottom.includes(id)) return "bottom";
  if ((ro.hidden ?? []).includes(id)) return "hidden";
  return null;
}

/** Is the Artifact panel a surface in this console (the plugin enabled + its view known)?
 *  railOrder is reconciled against the enabled plugins' views once runtime status resolves,
 *  so a disabled/uninstalled plugin's key is pruned from it. */
export function isArtifactViewAvailable(): boolean {
  return dockOf(ARTIFACT_VIEW_KEY) !== null;
}

export function useArtifactViewAvailable(): boolean {
  return useUI((s) => {
    const ro = s.railOrder;
    const k = ARTIFACT_VIEW_KEY;
    return ro.left.includes(k) || ro.right.includes(k) || ro.bottom.includes(k) || (ro.hidden ?? []).includes(k);
  });
}

export type OpenArtifactOptions = {
  /** An open the operator did NOT ask for (the agent just wrote the version). */
  auto?: boolean;
  /** The chat the live turn belongs to — an auto-open only fires for the chat on screen. */
  sessionId?: string;
};

/** Show `ref` in the Artifact panel and bring the panel on screen: queue the panel's
 *  `protoArtifact:select` (delivered once its page is listening — a collapsed dock mounts
 *  it only on this open), then route to it. Returns whether it opened.
 *
 *  An AUTO open (the live turn) is more careful than a click: never on a phone (ADR 0086 —
 *  the chip pushes the panel on a tap instead), never for a background tab's turn, and never
 *  when the panel shares chat's dock, where opening it would swap the conversation out from
 *  under the operator. */
export function openArtifactRef(ref: Pick<ArtifactRef, "id" | "version">, opts: OpenArtifactOptions = {}): boolean {
  const dock = dockOf(ARTIFACT_VIEW_KEY);
  if (!dock) return false;
  if (opts.auto) {
    if (isMobileViewport()) return false;
    let active: string | null = null;
    try {
      active = chatStore.getSnapshot().currentSessionId;
    } catch {
      active = null;
    }
    if (opts.sessionId && active && opts.sessionId !== active) return false;
    const chatDock = dockOf("chat") ?? "left";
    if (dock === chatDock || dock === "hidden") return false;
  }
  postToPluginView(ARTIFACT_VIEW_KEY, { type: "protoArtifact:select", id: ref.id, ver: ref.version });
  // flushSync: a collapsed dock UNMOUNTS its column (DS AppShell), so the panel's iframe only
  // exists after this commit — committing now starts its load (and so its ready ping) at once.
  try {
    flushSync(() => openView(ARTIFACT_VIEW_KEY));
  } catch {
    openView(ARTIFACT_VIEW_KEY);
  }
  return true;
}

/** The live-stream hook (registered with the chip): the agent just created or revised an
 *  artifact → open the panel on that version. Live turn only — history hydration and
 *  reattach never call it, so a reload never reopens the panel. */
export function onLiveArtifactRef(
  spec: { component: string; props: Record<string, unknown> },
  ctx: { sessionId?: string },
): void {
  if (spec.component !== ARTIFACT_REF_COMPONENT) return;
  const ref = artifactRefFromProps(spec.props);
  if (ref) openArtifactRef(ref, { auto: true, sessionId: ctx.sessionId });
}
