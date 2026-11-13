import { Button } from "@protolabsai/ui/primitives";
import { Maximize2 } from "lucide-react";
import { useEffect, useRef } from "react";

import { Markdown } from "../chat/LazyMarkdown";
import { openDocument } from "../docviewer";
import { relTime } from "../work/tone";
import { sourceLabel, stripWakeHeader, type FeedRow } from "./feedModel";

// One feed row's detail (docs/design/feed.md §5), rendered by ActivitySurface in the
// SAME dialog body — the list swaps for this view, no second dialog. Focus lands on the
// heading on open (§8); "← Feed" hands control back to the surface, which returns focus
// to the row that opened it. Like the list, nothing here dismisses, delivers or alerts —
// the only actions are going back and opening the shared reader.
export function FeedDetail({
  row,
  agentName,
  onBack,
}: {
  row: FeedRow;
  agentName: string;
  onBack: () => void;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => headingRef.current?.focus(), []);

  const entry = row.entry;
  // The queued row's "stimulus" is the inbox item itself; an entry carries the prompt it
  // answered. Either way the scheduler's internal wake header never shows (#5094).
  const stimulus = stripWakeHeader(entry ? entry.stimulus : row.item?.text);
  const source = entry ? sourceLabel(entry.origin, entry.trigger) : sourceLabel("inbox", row.item?.source);
  const time = relTime(row.sortAt);

  return (
    <div className="feed-detail" data-testid="feed-detail">
      <div className="feed-detail-head">
        <Button variant="ghost" size="sm" onClick={onBack}>
          ← Feed
        </Button>
        {entry ? (
          /* The open-in-reader action (ADR 0062) — the full entry in the same full-screen
             reader the chat report card opens. It lives here now, not on the list rows. */
          <Button
            icon
            variant="ghost"
            aria-label="Open in reader"
            title="Open in reader"
            onClick={() =>
              openDocument({
                title: source,
                subtitle:
                  [entry.task_id ? `task ${entry.task_id}` : "", time.tooltip].filter(Boolean).join(" · ") ||
                  undefined,
                content: stimulus
                  ? `> **In response to**\n>\n> ${stimulus.replace(/\n/g, "\n> ")}\n\n---\n\n${entry.text}`
                  : entry.text,
              })
            }
          >
            <Maximize2 size={14} />
          </Button>
        ) : null}
      </div>
      <h3 className="feed-detail-title" tabIndex={-1} ref={headingRef}>
        {row.title}
      </h3>
      <dl className="feed-detail-fields">
        <dt>What triggered it</dt>
        <dd>{source}</dd>
        <dt>When</dt>
        <dd>
          <span title={time.tooltip}>{time.text}</span>
        </dd>
        {stimulus ? (
          <>
            <dt>In response to</dt>
            <dd className="feed-detail-stimulus">{stimulus}</dd>
          </>
        ) : null}
        {row.kind === "queued" ? (
          <>
            <dt>Status</dt>
            {/* Mirrors the list row (feedModel queuedRow): once the automatic retry
               budget is spent no sweep ever runs this page again, so promising a run —
               or "nothing for you to do" — would be false. That page sits in Work ›
               Needs you, whose Delivery detail carries the one dismiss path. */}
            <dd>
              {row.item?.retries_exhausted
                ? "Queued, but the automatic retries are used up — it will not run on its own. It needs you in Work › Needs you."
                : `Queued. It runs when ${agentName} finishes what it's doing now. Nothing for you to do.`}
            </dd>
          </>
        ) : row.kind === "failed" ? (
          <>
            <dt>What went wrong</dt>
            <dd className="feed-detail-reason">{entry?.text}</dd>
          </>
        ) : (
          <>
            <dt>Response</dt>
            <dd className="feed-detail-response">
              <Markdown>{entry?.text ?? ""}</Markdown>
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}
