import { useEffect, useMemo, useRef, useState } from "react";
import { Link, Lock } from "lucide-react";
import { Alert, Spinner } from "@protolabsai/ui/data";
import { Badge, Button, Callout, Empty, type Status } from "@protolabsai/ui/primitives";
import { Tooltip } from "@protolabsai/ui/overlays";
import { Conversation, Message, TypingIndicator } from "@protolabsai/ui/ai";

import { api, ApiError } from "../lib/api";
import { textFromParts, type DurableChatTurn } from "../lib/api/a2aStream";
import { openWorkGroup } from "./ChatStatusRail";
import { Markdown } from "./Markdown";
import { ThreadJobs } from "./ThreadJobs";
import { conversationMeta, displayTitle } from "./conversation-title";
import { backToList, type ConversationRow } from "./conversations-store";

// The read-only thread view inside the Conversations tab (docs/design/other-conversations.md,
// slice C3, §3.4/§3.5/§5). ConversationsPane renders it in place of the list while the store
// holds an open thread id; the Conversations tab stays selected. A slim two-line header band
// (← Back + the focused displayTitle() + status Badge on one line, the meta line below — C9c),
// the thread's turns
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
  /** Continue in a new chat (§4.5): hands the open thread's id, title and origin to the caller,
   *  which opens a pre-filled `chat-` tab via `chatStore.continueConversation`. */
  onContinue?: (args: { contextId: string; title: string; origin: string }) => void;
}) {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [reloadKey, setReloadKey] = useState(0);
  const titleRef = useRef<HTMLHeadingElement>(null);

  const active = !!row?.active;
  const lastState = row?.last_state ?? null;
  const tone = threadTone(active, lastState);
  const parked = PARKED.test(lastState ?? "");
  // The FULL stored title stays the Tooltip label and the Continue pre-fill (§3.4 item 2d); the
  // h2 shows displayTitle(), which drops a redundant "From <caller>:" prefix using the row's
  // caller / on_behalf_of (C9a/C9b).
  const title = row?.title || "Conversation";
  const shownTitle = displayTitle(title, { caller: row?.caller, onBehalfOf: row?.on_behalf_of });
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

  // The meta line: "<origin without 'From'> · <d Mon>, <HH:mm> · N messages" (count only above 1),
  // the long form "From … · <d Mon> at <HH:mm> · N messages" hung on its Tooltip (§3.4 item 2 / §9).
  const meta = conversationMeta(row?.origin_label, row?.last_updated, row?.turn_count);

  const copyLink = () => {
    void navigator.clipboard?.writeText(`#/conversations/${threadId}`);
  };

  // The full read-only explanation (§3.4 item 1 / §7): it hangs on the lock's Tooltip and is the
  // lock's accessible name, so keyboard and screen-reader users reach it. The visible clause beside
  // the lock only says "Read-only. Replies start a new chat.".
  const lockHint = `You can’t type here. Replies go to a new chat, so ${caller} isn’t interrupted.`;

  return (
    <section className="conversation-reader" aria-label="Conversation, read-only">
      <header className="conversation-reader__head">
        {/* ← Back, the title and the status Badge share one line; the meta line sits below (§3.4
            item 2, §3.7). On the phone the app header owns ← List, so mobile-shell.css hides this
            in-band Back (keyed on `conversation-reader__back`). */}
        <div className="conversation-reader__titlerow">
          <Button
            variant="ghost"
            size="sm"
            className="conversation-reader__back"
            aria-label="Back to From others"
            title="Back to From others"
            onClick={() => backToList()}
          >
            ← Back
          </Button>
          <h2 className="conversation-reader__title" tabIndex={-1} ref={titleRef}>
            <Tooltip label={title}>
              <span tabIndex={0}>{shownTitle}</span>
            </Tooltip>
          </h2>
          {load.kind === "ready" && tone.badge && <Badge status={tone.tone}>{tone.badge}</Badge>}
        </div>
        {meta.text && (
          <p className="conversation-reader__origin">
            <Tooltip label={meta.tooltip}>
              <span>{meta.text}</span>
            </Tooltip>
          </p>
        )}
        {row && <ThreadJobs key={threadId} row={row} />}
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

      {/* The composer's slot — one read-only row, never an input (§3.4 item 1, §3.5). Lock + the
          short clause on the left; Copy link and Continue on the right. Hidden on the gone/failed
          dead ends, where Back to list / Try again are the only moves. On the phone the lock keeps
          its clause as "Read-only", Copy link goes icon-only, each a 44px target (mobile-shell.css). */}
      {load.kind !== "gone" && load.kind !== "failed" && (
        <div className="conversation-reader__footer">
          <div className="conversation-reader__footer-lede">
            <Tooltip label={lockHint}>
              <span
                className="conversation-reader__footer-lock"
                role="img"
                tabIndex={0}
                aria-label={lockHint}
              >
                <Lock size={14} aria-hidden />
              </span>
            </Tooltip>
            <span className="conversation-reader__footer-note">
              Read-only<span className="conversation-reader__footer-note-rest">. Replies start a new chat.</span>
            </span>
          </div>
          <div className="conversation-reader__footer-actions">
            <Tooltip label="Copy link">
              <Button
                variant="ghost"
                size="sm"
                className="conversation-reader__footer-copy"
                aria-label="Copy link"
                onClick={copyLink}
              >
                <Link size={14} aria-hidden />
                <span className="conversation-reader__footer-copy-label">Copy link</span>
              </Button>
            </Tooltip>
            <Button
              variant="primary"
              size="sm"
              onClick={() => onContinue?.({ contextId: threadId, title, origin: row?.origin_label ?? caller })}
            >
              Continue in a new chat
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
