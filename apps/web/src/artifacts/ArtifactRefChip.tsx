import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { useEffect } from "react";

import { api } from "../lib/api";
import { onTopic } from "../lib/events";
import {
  artifactRefFromProps,
  openArtifactRef,
  refName,
  refState,
  useArtifactViewAvailable,
  versionLabel,
  type RefState,
} from "./artifactRef";

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

const REF_STALE_MS = 15_000;

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
