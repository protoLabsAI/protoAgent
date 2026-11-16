import { useEffect, useRef, useState } from "react";
import { ChevronRight } from "lucide-react";
import { Alert, Skeleton, SkeletonGroup, StatusDot } from "@protolabsai/ui/data";
import { Badge, Button, Eyebrow, type Status } from "@protolabsai/ui/primitives";
import { Tooltip } from "@protolabsai/ui/overlays";

import { relTime } from "../work/tone";
import { chatStore } from "./chat-store";
import { ConversationReader } from "./ConversationReader";
import type { ConversationRow } from "./conversations-store";
import { openThread, retry, useConversations } from "./conversations-store";

// The read-only "Other conversations" list pane (docs/design/other-conversations.md, slice C2,
// §3.3/§3.6/§5). It fills the chat's main pane while the pinned Conversations tab is selected —
// a header (h2 + "Read-only" eyebrow + a muted explainer), a "For you" group, a collapsed
// "Automated · N" disclosure, and "Show older", with loading / failed / automated-only states.
// There is NO composer anywhere (§3.5): the agent isn't talking to the operator here. The thread
// view a row opens is slice C3; for now a row click just records the open thread id in the store.

// Parked waiting on the operator — "Needs you" (same spelling set as the store's live updates).
const PARKED = /input[-_]?required|auth[-_]?required/i;
const FAILED = /fail|error/i;

type RowTone = { word: string; tone: Status; badge: string | null };

/** Status in a dot + word + optional badge, from `active` / `last_state` (§4.2). Needs you wins
 *  over Running wins over Failed; idle/done show a neutral dot and no badge. */
function rowTone(row: ConversationRow): RowTone {
  if (PARKED.test(row.last_state ?? "")) return { word: "Needs you", tone: "warning", badge: "Needs you" };
  if (row.active) return { word: "Running", tone: "info", badge: "Running" };
  if (FAILED.test(row.last_state ?? "")) return { word: "Failed", tone: "error", badge: "Failed" };
  return { word: "Idle", tone: "neutral", badge: null };
}

/** One composite row button (§3.6): a status dot, the one-line title, a Badge when not idle, and
 *  a muted origin · time · count line (full timestamp in a Tooltip, count only above 1). 44 px
 *  minimum, DS focus ring, tokens only. Its accessible name is "<title>, <origin>, <status>,
 *  <relative time>" — set explicitly so the dot/badge/tooltip don't read it in pieces. */
function ConversationRowButton({ row }: { row: ConversationRow }) {
  const tone = rowTone(row);
  const t = row.last_updated ? relTime(row.last_updated) : { text: "", tooltip: "" };
  const openJobs = row.tasks?.filter((task) => task.status !== "closed").length ?? 0;
  const jobsLabel = openJobs ? `${openJobs} open job${openJobs === 1 ? "" : "s"}` : "";
  const name = `${row.title}, ${row.origin_label}, ${tone.word}${t.text ? `, ${t.text}` : ""}${jobsLabel ? `, ${jobsLabel}` : ""}`;
  return (
    <li className="conversations-row">
      <button
        type="button"
        className="conversations-row__btn"
        data-row-id={row.session_id}
        aria-label={name}
        onClick={() => openThread(row.session_id)}
      >
        {/* The dot pulses for a Running row — the pulse the TAB deliberately drops lives here. */}
        <StatusDot status={tone.tone} pulse={row.active} />
        <span className="conversations-row__main">
          <span className="conversations-row__head">
            <span className="conversations-row__title">{row.title}</span>
            {tone.badge && <Badge status={tone.tone}>{tone.badge}</Badge>}
          </span>
          <span className="conversations-row__meta">
            {row.origin_label}
            {t.text ? (
              <>
                {" · "}
                <Tooltip label={t.tooltip}>
                  <span>{t.text}</span>
                </Tooltip>
              </>
            ) : null}
            {row.turn_count > 1 ? ` · ${row.turn_count} messages` : ""}
            {jobsLabel ? ` · ${jobsLabel}` : ""}
          </span>
        </span>
      </button>
    </li>
  );
}

export function ConversationsPane({ agentName }: { agentName: string }) {
  const conv = useConversations();
  const openThreadId = conv.openThreadId;
  // Automated opens by default only in the automated-only state (§5); otherwise it is collapsed
  // and the operator toggles it. Controlled so the "opens by default" case can't be collapsed
  // shut onto an empty For-you group.
  const automatedOnly = conv.forYou.length === 0 && conv.automated.length > 0;
  const [automatedOpen, setAutomatedOpen] = useState(false);
  const showAutomated = automatedOpen || automatedOnly;

  // Back to list returns focus to the row the thread was opened from (§3.4/§7). The list unmounts
  // while the reader shows and remounts on return, so capture the id on open and refocus the
  // matching row button after the list is back in the DOM (not the stale element, which is gone).
  const listRef = useRef<HTMLElement>(null);
  const lastOpenRef = useRef<string | null>(null);
  useEffect(() => {
    if (openThreadId) {
      lastOpenRef.current = openThreadId;
      return;
    }
    const id = lastOpenRef.current;
    if (!id) return;
    lastOpenRef.current = null;
    const rows = listRef.current?.querySelectorAll<HTMLButtonElement>(".conversations-row__btn");
    rows?.forEach((btn) => {
      if (btn.dataset.rowId === id) btn.focus();
    });
  }, [openThreadId]);

  // An open thread shows the read-only reader in the same pane (§3.4); the tab stays selected.
  // Continue in a new chat (§4.5) opens a pre-filled chat tab and leaves the Conversations stage.
  if (openThreadId) {
    const openRow = conv.rows.find((r) => r.session_id === openThreadId);
    return (
      <ConversationReader
        threadId={openThreadId}
        row={openRow}
        agentName={agentName}
        onContinue={(args) => chatStore.continueConversation(args)}
      />
    );
  }

  return (
    <section className="conversations-pane" aria-label="From others" ref={listRef}>
      <header className="conversations-pane__head">
        <Eyebrow>Read-only</Eyebrow>
        <h2 className="conversations-pane__title">From others</h2>
        <p className="conversations-pane__sub">
          Threads that other people and agents started with {agentName}. You can&rsquo;t type here.
        </p>
      </header>

      <div className="conversations-pane__body">
        {conv.status === "loading" ? (
          // Deep-link first load (§5): the tab is normally never drawn while loading, so this
          // only shows when a link forced it open before the list arrived.
          <SkeletonGroup className="conversations-skeletons" aria-label="Loading threads from others">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} height={44} />
            ))}
          </SkeletonGroup>
        ) : conv.status === "error" ? (
          <Alert
            status="error"
            action={
              <Button size="sm" variant="ghost" onClick={() => retry()}>
                Try again
              </Button>
            }
          >
            Couldn&rsquo;t load threads from others. Check the connection, then try again.
          </Alert>
        ) : (
          <>
            <section className="conversations-group" aria-label="For you">
              <Eyebrow>For you</Eyebrow>
              {conv.forYou.length === 0 ? (
                <p className="conversations-group__empty">Nothing started for you this week.</p>
              ) : (
                <ul className="conversations-list">
                  {conv.forYou.map((row) => (
                    <ConversationRowButton key={row.session_id} row={row} />
                  ))}
                </ul>
              )}
            </section>

            {conv.automated.length > 0 && (
              <section className="conversations-group">
                <button
                  type="button"
                  className="conversations-disclosure"
                  aria-expanded={showAutomated}
                  aria-controls="conversations-automated"
                  onClick={() => setAutomatedOpen((v) => !v)}
                >
                  <ChevronRight
                    className={`conversations-disclosure__chevron${showAutomated ? " is-open" : ""}`}
                    size={14}
                    aria-hidden
                  />
                  <span>Automated &middot; {conv.automated.length}</span>
                </button>
                <ul id="conversations-automated" className="conversations-list" hidden={!showAutomated}>
                  {conv.automated.map((row) => (
                    <ConversationRowButton key={row.session_id} row={row} />
                  ))}
                </ul>
              </section>
            )}

            {conv.hasOlder && (
              <div className="conversations-older">
                <Button size="sm" variant="ghost" loading={conv.loadingOlder} onClick={() => conv.loadOlder()}>
                  Show older
                </Button>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
