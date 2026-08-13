// The pre-publish review (#2179 P2, #2682) — #2179's own "blocking design question":
// preview exactly what becomes public BEFORE anything leaves the instance. Mounted once
// (see ChatSurface); which session it's showing, if any, lives in publishDialogStore so
// the slash command and the tab context-menu item can open it without a prop chain.
//
// Reuses the real rendering primitives where they fit (Message bubble chrome, Markdown,
// ToolCalls — the same components the live chat uses) rather than a second renderer. No
// dedicated artifact-preview component exists anywhere in the console yet, so artifacts
// get a small purpose-built summary card here: kind/title/version/availability + a
// trimmed content snippet, deliberately NOT a full sandboxed render (that's a hosted-
// viewer concern, #2685, not this confirm step's job).
import { Message } from "@protolabsai/ui/ai";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { Spinner } from "@protolabsai/ui/data";
import { useState } from "react";

import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import type { ChatBundlePart } from "../lib/types";
import { chatStore } from "./chat-store";
import { Markdown } from "./LazyMarkdown";
import { closePublishDialog, usePublishDialogSessionId } from "./publishDialogStore";
import { confirmPublish } from "./publishChat";
import { ToolCalls } from "./ToolCalls";

const SNIPPET_LEN = 240;

function titleOf(sessionId: string): string | undefined {
  return chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)?.title;
}

function ArtifactSummary({ artifact }: { artifact: NonNullable<Extract<ChatBundlePart, { kind: "tool_call" }>["artifact"]> }) {
  if (!artifact.available) {
    return (
      <div className="pl-publish-artifact pl-publish-artifact--unavailable">
        <strong>{artifact.title || artifact.artifact_kind || "Artifact"}</strong> — not included
        {artifact.file_meta ? ` (${artifact.file_meta.filename}, ${artifact.file_meta.mime})` : ""}: {artifact.reason}
      </div>
    );
  }
  const snippet = artifact.content.length > SNIPPET_LEN ? `${artifact.content.slice(0, SNIPPET_LEN)}…` : artifact.content;
  return (
    <div className="pl-publish-artifact">
      <strong>
        {artifact.title || "Untitled"} <span className="pl-publish-artifact-kind">{artifact.artifact_kind} · v{artifact.version}</span>
      </strong>
      <pre className="pl-publish-artifact-snippet">{snippet}</pre>
    </div>
  );
}

function PartsView({ parts }: { parts: ChatBundlePart[] }) {
  const text = parts.filter((p): p is Extract<ChatBundlePart, { kind: "text" }> => p.kind === "text").map((p) => p.text).join("\n\n");
  const calls = parts.filter((p): p is Extract<ChatBundlePart, { kind: "tool_call" }> => p.kind === "tool_call");
  return (
    <>
      {text ? <Markdown>{text}</Markdown> : null}
      {calls.length ? (
        <ToolCalls
          calls={calls.map((c) => ({ id: c.id, name: c.name, input: JSON.stringify(c.input ?? {}), output: c.output, status: "done" as const }))}
          flat
        />
      ) : null}
      {calls.filter((c) => c.artifact).map((c) => (
        <ArtifactSummary key={c.id} artifact={c.artifact!} />
      ))}
    </>
  );
}

export function PublishDialog() {
  const sessionId = usePublishDialogSessionId();
  const [publishing, setPublishing] = useState(false);
  const title = sessionId ? titleOf(sessionId) : undefined;

  const preview = useQuery({
    queryKey: ["publish-preview", sessionId, title],
    queryFn: () => api.fetchPublishPreview(sessionId as string, title),
    enabled: sessionId !== null,
  });

  const handleClose = () => {
    if (publishing) return; // an in-flight publish must finish (or fail) on its own
    closePublishDialog();
  };

  return (
    <ConfirmDialog
      open={sessionId !== null}
      title="Publish this chat?"
      confirmLabel={publishing ? "Publishing…" : "Publish"}
      onConfirm={() => {
        // ConfirmDialog has no disabled-state affordance — guard here instead. A click
        // before the preview loads (or while a publish is already in flight) is a no-op,
        // not a crash or a double-submit.
        if (!sessionId || publishing || !preview.data?.found) return;
        setPublishing(true);
        void confirmPublish(sessionId, title).finally(() => {
          setPublishing(false);
          closePublishDialog();
        });
      }}
      onClose={handleClose}
    >
      {preview.isLoading ? (
        <p style={{ display: "flex", alignItems: "center", gap: 8, margin: 0 }}>
          <Spinner size={15} /> Building the preview…
        </p>
      ) : preview.isError ? (
        <p style={{ margin: 0 }}>Couldn't build the preview — {errMsg(preview.error)}.</p>
      ) : !preview.data?.found ? (
        <p style={{ margin: 0 }}>{preview.data?.message ?? "Nothing to publish yet."}</p>
      ) : (
        <div className="pl-publish-preview">
          <p style={{ margin: "0 0 8px" }}>
            This becomes a <strong>public, unauthenticated link</strong>. Anyone with it can read everything below —
            review before confirming.
          </p>
          {preview.data.redactions.length ? (
            <p className="pl-publish-redactions">
              <strong>{preview.data.redactions.length} secret pattern(s) redacted:</strong>{" "}
              {preview.data.redactions.join(", ")} — this is a safety net, not a guarantee; read through before publishing.
            </p>
          ) : null}
          <div className="pl-publish-messages">
            {preview.data.manifest?.messages.map((m, i) => (
              <Message role={m.role} key={i}>
                <PartsView parts={m.parts} />
              </Message>
            ))}
          </div>
        </div>
      )}
    </ConfirmDialog>
  );
}
