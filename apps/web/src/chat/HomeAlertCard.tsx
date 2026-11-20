import { Badge, Button } from "@protolabsai/ui/primitives";
import { Message } from "@protolabsai/ui/ai";
import { Bell, CalendarClock, MessageCircleQuestion, TriangleAlert } from "lucide-react";
import type { MouseEvent } from "react";

import { openNotice } from "../app/NoticeClickWatch";
import { openView } from "../app/palette/nav";
import type { ChatMessage, HomeEntry } from "../lib/types";
import { useUI } from "../state/uiStore";
import { openWorkGroup } from "./ChatStatusRail";
import { quoteEntry, replyContext, replyToHomeEntry, type HomeReply } from "./homeReply";

// A Home entry (ADR 0135 D9, #5545): a brief, an alert or a decision pointer the server wrote
// into Home's thread (`graph/home_entries.py`). It renders as ONE card — icon, one-line title,
// "Urgent" for high importance, the time, and a link to what raised it — so a stopped fleet
// member or a waiting decision is readable without the reference text the model sees.

/** Opening of every Home entry's text (`home_entries.HOME_ENTRY_PREFIX`). */
const ENTRY_PREFIX = "[Home entry";
const DECISION_PREFIX = "decision-"; // graph/decisions.py: notice_id = f"decision-{rec.id}"
const LINK_ONLY = /^(https?:\/\/|\/)\S+$/;

/** Split an entry's stored text into what the card shows. The server writes
 *  `"[Home entry: …]\n<title>\n\n<body>\n\n<link>"`; the bracketed head is for the model and
 *  is dropped, and a closing paragraph that is only a link becomes the source link. */
export function homeEntryText(content: string): { title: string; body: string; link: string | null } {
  const lines = (content ?? "").trim().split("\n");
  if (lines[0]?.startsWith(ENTRY_PREFIX)) lines.shift();
  const paragraphs = lines.join("\n").trim().split(/\n{2,}/);
  const [first = "", ...restOfFirst] = (paragraphs.shift() ?? "").split("\n");
  if (restOfFirst.length) paragraphs.unshift(restOfFirst.join("\n"));
  let link: string | null = null;
  const last = paragraphs[paragraphs.length - 1]?.trim();
  if (last && LINK_ONLY.test(last)) {
    link = last;
    paragraphs.pop();
  }
  return { title: first.trim(), body: paragraphs.join("\n\n").trim(), link };
}

/** A link the card may put in an `href`: http(s) only, so stored text can never smuggle a
 *  `javascript:` URL into the console. */
function safeHref(link: string | null): string | null {
  if (!link) return null;
  try {
    const url = new URL(link, window.location.href);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

/** Whether the entry points at a decision waiting for the operator. */
export function isDecisionPointer(home: HomeEntry): boolean {
  return home.kind === "question" || home.source === "decision" || (home.notice_id ?? "").startsWith(DECISION_PREFIX);
}

type SourceTarget = { label: string; href: string; open?: () => void };

/** Where the card's source link goes. `open` is the in-console route (taken on a plain
 *  click); `href` is the same place as an address, for a new tab or a copied link. Null when
 *  the entry names nothing this console can open. */
function sourceTarget(home: HomeEntry, link: string | null): SourceTarget | null {
  const carried = safeHref(link);
  if (isDecisionPointer(home)) {
    const id = home.notice_id ?? "";
    const known = id.startsWith(DECISION_PREFIX) && id.length > DECISION_PREFIX.length;
    return {
      label: "Decision",
      href: known ? `?notice=${encodeURIComponent(id)}` : (carried ?? "./"),
      // openNotice hands a decision another agent raised to THAT agent's console.
      open: () => {
        if (known) void openNotice(id);
        else openWorkGroup("needs-you");
      },
    };
  }
  if (home.source === "fleet-health") {
    return { label: "Fleet member", href: carried ?? "./", open: () => useUI.getState().openGlobalSettings("fleet") };
  }
  if (home.source === "scheduler") return { label: "Job run", href: carried ?? "./", open: () => openView("work") };
  if (home.source === "watch") return { label: "Watch", href: carried ?? "./", open: () => openView("work") };
  return carried ? { label: home.source === "spend-cap" ? "Spend cap" : "Source", href: carried } : null;
}

function entryTime(createdAt?: number): { label: string; full: string; iso: string } | null {
  if (typeof createdAt !== "number" || !Number.isFinite(createdAt) || createdAt <= 0) return null;
  const d = new Date(createdAt);
  if (Number.isNaN(d.getTime())) return null;
  return {
    label: d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }),
    full: d.toLocaleString([], { dateStyle: "full", timeStyle: "medium" }),
    iso: d.toISOString(),
  };
}

/** What "Reply" puts in Home's composer for an entry: the visible quote, plus the hidden
 *  reference that tells the agent which entry the operator is answering. */
export function homeEntryReply(message: ChatMessage, home: HomeEntry): HomeReply {
  const { title, body } = homeEntryText(message.content);
  const time = entryTime(message.createdAt);
  const facts = [
    `kind ${home.kind}`,
    home.source ? `source ${home.source}` : "",
    home.notice_id ? `notice ${home.notice_id}` : "",
    time ? `written ${time.iso}` : "",
  ].filter(Boolean);
  return {
    quote: quoteEntry(title || "Notice", body),
    context: replyContext(`the Home entry "${title || "Notice"}" (${facts.join(", ")})`),
  };
}

export function HomeAlertCard({
  message,
  home,
  sessionId,
}: {
  message: ChatMessage;
  home: HomeEntry;
  /** Home's session — the chat this card sits in. Reply writes into its composer; without it
   *  (a render outside a chat) the card offers no Reply. */
  sessionId?: string;
}) {
  const { title, body, link } = homeEntryText(message.content);
  const urgent = home.importance === "high";
  const decision = isDecisionPointer(home);
  // A decision is answered on its own card ("Answer"), never by a free-text reply here.
  const canReply = !decision && Boolean(sessionId);
  const source = sourceTarget(home, link);
  const time = entryTime(message.createdAt);
  const Icon = decision ? MessageCircleQuestion : urgent ? TriangleAlert : home.kind === "review" ? CalendarClock : Bell;
  const heading = title || (decision ? "A decision is waiting" : "Notice");
  const onSource = (event: MouseEvent<HTMLAnchorElement>) => {
    // A modified click (new tab, new window) keeps the browser's own behavior on the href.
    if (!source?.open || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    source.open();
  };
  return (
    // Always the neutral system row: the entry is stored as a human message so the model
    // reads it as input, but it is not the operator's words and must not render as their bubble.
    <Message role="system" className="pl-home-alert">
      <article className="pl-home-alert-card">
        <div className="pl-home-alert-head">
          <Icon size={14} aria-hidden className={`pl-home-alert-icon${urgent ? " pl-home-alert-icon--urgent" : ""}`} />
          <h3 className="pl-home-alert-title" title={heading}>
            {heading}
          </h3>
          {urgent ? <Badge>Urgent</Badge> : null}
          {time ? (
            <time className="pl-chip-meta" dateTime={time.iso} title={time.full}>
              {time.label}
            </time>
          ) : null}
        </div>
        {body ? <p className="pl-home-alert-body">{body}</p> : null}
        {source || decision || canReply ? (
          <div className="pl-home-alert-actions">
            {source ? (
              <a className="pl-link" href={source.href} onClick={onSource}>
                {source.label}
              </a>
            ) : null}
            {decision ? (
              <Button variant="primary" size="sm" onClick={() => source?.open?.()}>
                Answer
              </Button>
            ) : null}
            {canReply ? (
              <Button variant="ghost" size="sm" onClick={() => replyToHomeEntry(sessionId!, homeEntryReply(message, home))}>
                Reply
              </Button>
            ) : null}
          </div>
        ) : null}
      </article>
    </Message>
  );
}
