/**
 * Background agents — the Work view's §2.9 section (#4955 comment 6739622302).
 *
 * A background job is a detached subagent run (ADR 0050/0051): `BackgroundJobDTO`, whose raw
 * status is one of `running | completed | failed | canceled` (src/background/store.py). The
 * list is a column of `WorkRow`s; opening one renders a `WorkDetail` with the job's field
 * table, the Markdown result (clamped), and — while it's still running — a Cancel job action.
 *
 * Registration is a module side effect: importing this file adds the "background" section to
 * the `WorkView` registry, so a renderer is installed exactly once per page.
 */
import { useWorkActionSize } from "../../shell/SurfaceFrame";
import { type CSSProperties, useState } from "react";
import { Button, Callout } from "@protolabsai/ui/primitives";
import { WorkEmpty as Empty } from "../WorkEmpty";
import { Alert, Skeleton } from "@protolabsai/ui/data";
import { Drawer } from "@protolabsai/ui/overlays";
import { registerWorkSection, type WorkSectionContext } from "../WorkView";
import { WorkRow } from "../WorkRow";
import { WorkDetail, type WorkField } from "../WorkDetail";
import { relTime } from "../tone";
import type { WorkItem } from "../useWorkItems";
import type { BackgroundJobDTO } from "../../lib/types";
import { api } from "../../lib/api";
import { jobFailureReason } from "../../app/background-jobs";
import { Markdown } from "../../chat/LazyMarkdown";
import { chatStore, useChatState } from "../../chat/chat-store";
import { BackgroundToolFeed, useBackgroundProgress } from "../../app/BackgroundToolFeed";
import { CopyResult } from "../../app/BackgroundJobs";
import { SubagentsPanel } from "../../app/SubagentsPanel";

/** A subagent slug (`research-assistant`, `delegate`) as a human display name — never the
 *  raw slug (§2.9). Hyphens/underscores become spaces, each word is capitalized. */
export function roleLabel(slug: string): string {
  const words = (slug || "").trim().replace(/[-_]+/g, " ").trim();
  if (!words) return "Subagent";
  return words.replace(/\b\w/gu, (c) => c.toUpperCase());
}

/** Clamp the result Markdown to eight lines until the reader expands it (§2.9). */
const CLAMP_8: CSSProperties = {
  display: "-webkit-box",
  WebkitBoxOrient: "vertical",
  WebkitLineClamp: 8,
  overflow: "hidden",
};

function ResultText({ text }: { text: string }) {
  const size = useWorkActionSize();
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="work-background__result">
      <CopyResult text={text} size={size} />
      <div style={expanded ? undefined : CLAMP_8}>
        <Markdown>{text}</Markdown>
      </div>
      <Button variant="ghost" size={size} onClick={() => setExpanded((v) => !v)}>
        {expanded ? "Show less" : "Show full text"}
      </Button>
    </div>
  );
}

/** "From {chat title}" — a link to the originating chat when it's an open local tab, else a
 *  plain label. Never the raw session id (§2.9). There's no fetch-and-open path for a chat
 *  that was never opened in this browser (#2692), so an unknown session stays a plain label. */
function FromChat({ sessionId }: { sessionId: string }) {
  const size = useWorkActionSize();
  const { sessions } = useChatState();
  const session = sessions.find((s) => s.id === sessionId);
  if (!session) return <span className="work-background__from">another chat</span>;
  return (
    <Button variant="ghost" size={size} onClick={() => chatStore.switchSession(sessionId)}>
      {session.title}
    </Button>
  );
}

function BackgroundDetail({ item, onDone }: { item: WorkItem; onDone: () => void }) {
  const job = item.raw as BackgroundJobDTO;
  const tools = useBackgroundProgress((state) => state.jobs[job.id]);
  const reason = jobFailureReason(job);
  const fields: WorkField[] = [
    { label: "Role", value: roleLabel(job.subagent_type) },
    { label: "From", value: job.origin_session ? <FromChat sessionId={job.origin_session} /> : null },
    { label: "Started", value: job.created_at ? relTime(job.created_at).text : null },
    { label: "Finished", value: job.completed_at ? relTime(job.completed_at).text : null },
    // Only a failed or cancelled job explains why it ended — a completed one never does.
    { label: "Why it ended", value: reason ? `Stopped: ${reason}` : null },
    { label: "Tool activity", value: tools?.length ? <BackgroundToolFeed tools={tools} /> : null },
    { label: "Result", value: job.result ? <ResultText text={job.result} /> : null },
  ];
  return (
    <WorkDetail
      title={item.title || "Background agent"}
      status={item.status}
      fields={fields}
      actions={
        job.status === "running"
          ? [
              {
                id: "cancel",
                label: "Cancel job…",
                destructive: true,
                confirm: {
                  prompt: "Cancel this background job?",
                  explanation: "The agent stops where it is; work it hasn't saved is lost.",
                  confirmLabel: "Cancel job",
                  keepLabel: "Keep running",
                },
                run: async () => {
                  await api.stopBackground(job.id);
                  onDone();
                },
              },
            ]
          : []
      }
      technical={{
        "Job id": job.id,
        Subagent: job.subagent_type || "",
        Status: job.status,
        "Origin session": job.origin_session || "",
      }}
    />
  );
}

function BackgroundList({ ctx }: { ctx: WorkSectionContext }) {
  const source = ctx.sources.background;
  const [managing, setManaging] = useState(false);
  return (
    <div className="work-background">
      {source.status === "error" ? (
        <Alert status="warning">
          Couldn&apos;t load background agents.{" "}
          <Button size="sm" onClick={source.retry}>
            Retry
          </Button>
        </Alert>
      ) : source.status === "stale" ? (
        <Callout tone="warning">
          Background agents may be out of date.{" "}
          <Button size="sm" onClick={source.retry}>
            Retry
          </Button>
        </Callout>
      ) : null}
      {source.status === "loading" ? (
        <div aria-label="Loading background agents" aria-busy="true">
          <Skeleton lines={3} />
        </div>
      ) : source.items.length === 0 && source.status !== "error" ? (
        <Empty title="No background agents" description="Background work appears here when an agent starts it." />
      ) : (
        <ul className="work-group">
          {source.items.map((item) => (
            <li key={`${item.kind}:${item.id}`}>
              <WorkRow
                status={item.status}
                title={item.title}
                meta={roleLabel((item.raw as BackgroundJobDTO).subagent_type)}
                focusKey={`${item.kind}:${item.id}`}
                onOpen={() => ctx.onOpen(item)}
              />
            </li>
          ))}
        </ul>
      )}
      {/* Managing subagents is a SEPARATE surface (Q14) — a link to the existing panel, never
          merged into this list. */}
      <p className="work-background__manage">
        <Button variant="ghost" size="sm" onClick={() => setManaging(true)}>
          Manage subagents
        </Button>
      </p>
      <Drawer open={managing} onClose={() => setManaging(false)} width="min(540px, 96vw)" title="Subagents">
        {managing ? <SubagentsPanel /> : null}
      </Drawer>
    </div>
  );
}

registerWorkSection("background", {
  label: "Background agents",
  group: "doing-now",
  render(ctx) {
    const itemId = ctx.state.itemId;
    if (!itemId) return <BackgroundList ctx={ctx} />;
    const item = ctx.sources.background.items.find(
      (i) => `${i.kind}:${i.id}` === itemId || i.id === itemId,
    );
    if (!item) return null;
    return <BackgroundDetail item={item} onDone={ctx.onBack} />;
  },
});
