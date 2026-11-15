import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Spinner } from "@protolabsai/ui/data";
import { Badge, Button, Callout, Empty, Eyebrow, type Status } from "@protolabsai/ui/primitives";
import { Conversation, Message, TypingIndicator } from "@protolabsai/ui/ai";

import { api, ApiError } from "../lib/api";
import { textFromParts, type DurableChatTurn } from "../lib/api/a2aStream";
import { relTime } from "../work/tone";
import { openWorkGroup } from "./ChatStatusRail";
import { Markdown } from "./Markdown";
import { backToList, type ConversationRow } from "./conversations-store";

// The read-only thread view inside the Conversations tab (docs/design/other-conversations.md,
// slice C3, §3.4/§3.5/§5). ConversationsPane renders it in place of the list while the store
// holds an open thread id; the Conversations tab stays selected. A header band (Back to list,
// "Conversation · read-only", the focused title, status Badge, origin line), the thread's turns
// (`Conversation`/`Message`/`Markdown`, each labelled with its speaker), and — where a composer
// would sit — a footer bar explaining why there's no composer (§3.5). There is NO composer here.
//
// Two reads back the view: `chatApi.chatSession` confirms the thread still exists (a 404 is the
// "gone" state, §5) and `chatApi.chatSessionTurns` is the body. Live status (Running / Needs you /
// Failed) comes from the list row the pane passes down — it stays bus-fresh while the reader is
// open, so the badge and the running TypingIndicator track the thread without re-reading.

// Same spelling sets as the list (ConversationsPane / the store's live updates, §4.2).
const PARKED = /input[-_]?required|auth[-_]?required/i;
const FAILED = /fail|error/i;

type Tone = { word: string; tone: Status; badge: string | null };

/** Status word + DS tone + Badge label from `active` / `last_state` (§4.2). Needs you wins over
 *  Running wins over Failed; idle/done show no badge. */
function threadTone(active: boolean, lastState: string | null | undefined): Tone {
  if (PARKED.test(lastState ?? "")) return { word: "Needs you", tone: "warning", badge: "Needs you" };
  if (active) return { word: "Running", tone: "info", badge: "Running" };
  if (FAILED.test(lastState ?? "")) return { word: "Failed", tone: "error", badge: "Failed" };
  return { word: "Idle", tone: "neutral", badge: null };
}

/** The A2A role of a history message is a user one (proto enum name or the JSON spelling). */
function isUserRole(role?: string): boolean {
  return (role || "").includes("USER") || role === "user";
}

/** The bare caller name for a message label and the footer note — the row's origin label with
 *  its leading "From " / "Delegation from " dropped ("From Claude Code for Josh" → "Claude Code
 *  for Josh", "Delegation from protoEngineer" → "protoEngineer"). */
function callerName(originLabel: string | undefined): string {
  const stripped = (originLabel ?? "").replace(/^(from|delegation from)\s+/i, "").trim();
  return stripped || "the caller";
}

type ReaderMessage = { id: string; role: "user" | "assistant"; speaker: string; text: string };

/** Flatten the durable turns into speaker-labelled messages (§3.4): each turn's caller message
 *  (the first user-role history entry) then the agent's reply (`turn.text`, the joined assistant
 *  output). Empty sides are skipped so a parked turn awaiting input doesn't render a blank bubble. */
function messagesFromTurns(turns: DurableChatTurn[], caller: string, agentName: string): ReaderMessage[] {
  const out: ReaderMessage[] = [];
  for (const turn of turns) {
    const history = turn.history ?? [];
    const userMsg = history.find((m) => isUserRole(m.role));
    const userText = userMsg ? textFromParts(userMsg.parts) : "";
    if (userText.trim()) out.push({ id: `${turn.task_id}:u`, role: "user", speaker: caller, text: userText });
    const agentText =
      turn.text?.trim() ||
      textFromParts(history.filter((m) => !isUserRole(m.role)).flatMap((m) => m.parts ?? []));
    if (agentText.trim()) out.push({ id: `${turn.task_id}:a`, role: "assistant", speaker: agentName, text: agentText });
  }
  return out;
}

type Load =
  | { kind: "loading" }
  | { kind: "ready"; turns: DurableChatTurn[] }
  | { kind: "gone" }
  | { kind: "failed" };

export function ConversationReader({
  threadId,
  row,
  agentName,
  onContinue,
}: {
  threadId: string;
  /** The live list row for this thread (title, origin, bus-fresh status), when the list holds
   *  it — which it does whenever a row was clicked to open the reader. */
  row?: ConversationRow;
  agentName: string;
  /** Continue in a new chat (§4.5) — wired in slice C4; a no-op until then. */
  onContinue?: () => void;
}) {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [reloadKey, setReloadKey] = useState(0);
  const titleRef = useRef<HTMLHeadingElement>(null);

  const active = !!row?.active;
  const lastState = row?.last_state ?? null;
  const tone = threadTone(active, lastState);
  const parked = PARKED.test(lastState ?? "");
  const title = row?.title || "Conversation";
  const caller = callerName(row?.origin_label);

  // Opening a thread moves focus to its title (§7, tabindex -1), on open and on switch.
  useEffect(() => {
    titleRef.current?.focus();
  }, [threadId]);

  // Load the thread: the summary first (its 404 is the gone state), then the turns body. A
  // network/5xx on either is the failed state. `active` is a dep so a running turn finishing
  // (the row flips active→false over the bus) pulls the completed answer in without a re-open.
  useEffect(() => {
    let cancelled = false;
    setLoad((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
    (async () => {
      try {
        await api.chatSession(threadId);
      } catch (err) {
        if (cancelled) return;
        setLoad(err instanceof ApiError && err.status === 404 ? { kind: "gone" } : { kind: "failed" });
        return;
      }
      try {
        const detail = await api.chatSessionTurns(threadId);
        if (!cancelled) setLoad({ kind: "ready", turns: detail.turns ?? [] });
      } catch {
        if (!cancelled) setLoad({ kind: "failed" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [threadId, active, reloadKey]);

  const messages = useMemo(
    () => (load.kind === "ready" ? messagesFromTurns(load.turns, caller, agentName) : []),
    [load, caller, agentName],
  );

  const time = row?.last_updated ? relTime(row.last_updated) : null;
  const count = row?.turn_count ?? 0;
  const originLine = [row?.origin_label, time?.text, count > 1 ? `${count} messages` : ""]
    .filter(Boolean)
    .join(" · ");

  const copyLink = () => {
    void navigator.clipboard?.writeText(`#/conversations/${threadId}`);
  };

  return (
    <section className="conversation-reader" aria-label="Conversation">
      <header className="conversation-reader__head">
        <div className="conversation-reader__back">
          <Button variant="ghost" size="sm" onClick={() => backToList()}>
            ← Back to list
          </Button>
        </div>
        <Eyebrow>Conversation · read-only</Eyebrow>
        <div className="conversation-reader__titlerow">
          <h2 className="conversation-reader__title" tabIndex={-1} ref={titleRef}>
            {title}
          </h2>
          {load.kind === "ready" && tone.badge && <Badge status={tone.tone}>{tone.badge}</Badge>}
        </div>
        {originLine && <p className="conversation-reader__origin">{originLine}</p>}
      </header>

      <div className="conversation-reader__body">
        {load.kind === "loading" ? (
          <div className="conversation-reader__loading" aria-label="Loading conversation">
            <Spinner />
          </div>
        ) : load.kind === "gone" ? (
          <Empty
            title="This conversation is no longer stored."
            description="The agent keeps conversations for a limited time. This one has been cleared."
            action={
              <Button variant="ghost" size="sm" onClick={() => backToList()}>
                Back to list
              </Button>
            }
          />
        ) : load.kind === "failed" ? (
          <Alert
            status="error"
            action={
              <Button size="sm" variant="ghost" onClick={() => setReloadKey((k) => k + 1)}>
                Try again
              </Button>
            }
          >
            Couldn&rsquo;t load this conversation. Try again.
          </Alert>
        ) : (
          <>
            {parked && (
              <Callout tone="warning">
                <div className="conversation-reader__needs">
                  <span>This conversation is waiting for your answer.</span>
                  <Button variant="ghost" size="sm" onClick={() => openWorkGroup("needs-you")}>
                    Answer in Needs you
                  </Button>
                </div>
              </Callout>
            )}
            <Conversation className="conversation-reader__turns" autoScroll={false}>
              {messages.map((m) => (
                <Message key={m.id} role={m.role} origin={{ label: m.speaker }}>
                  <Markdown>{m.text}</Markdown>
                </Message>
              ))}
              {/* Running: the last message is the agent still working on its reply (§5). */}
              {active && (
                <Message role="assistant" origin={{ label: agentName }}>
                  <TypingIndicator label={`${agentName} is replying`} />
                </Message>
              )}
            </Conversation>
          </>
        )}
      </div>

      {/* The composer's slot — a read-only note, never an input (§3.5). Hidden on the gone/failed
          dead ends, where Back to list / Try again are the only moves. */}
      {load.kind !== "gone" && load.kind !== "failed" && (
        <div className="conversation-reader__footer">
          <p className="conversation-reader__footer-note">
            You can&rsquo;t type here. Replies go to a new chat, so {caller} isn&rsquo;t interrupted.
          </p>
          <div className="conversation-reader__footer-actions">
            <Button variant="primary" size="sm" onClick={() => onContinue?.()}>
              Continue in a new chat
            </Button>
            <Button variant="ghost" size="sm" onClick={copyLink}>
              Copy link
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
