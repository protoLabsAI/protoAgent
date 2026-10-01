import "./tool-calls.css";
import {
  Calculator,
  Clock,
  Database,
  Globe,
  Hourglass,
  Network,
  PauseCircle,
  Search,
  SlidersHorizontal,
  Square,
  Wrench,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Fragment } from "react";
import type { ReactNode } from "react";

import { ToolCard, ToolCardList, ToolCardSummary, ToolSection } from "@protolabsai/ui/tool-card";

import { tokens } from "../lib/format";
import type { ToolCall } from "../lib/types";
import { DelegateProgressList } from "./DelegateProgressView";
import { isCancelledDelegation } from "./dismissedToolCalls";
import { useUI } from "../state/uiStore";
import { SHOW_ELAPSED_AFTER_MS, formatElapsed, useElapsed } from "./elapsed";
import { ToolValue } from "./tool-renderers";
import { CHARS_PER_TOKEN, toolCostTokens } from "./toolCost";
import { humanizeSeconds, parseWaitInput } from "./waitInfo";

/** Map a tool name to a recognizable icon; falls back to a generic wrench. */
function iconFor(name: string): LucideIcon {
  if (name === "calculator") return Calculator;
  if (name === "web_search") return Search;
  if (name === "fetch_url") return Globe;
  if (name === "current_time") return Clock;
  if (name === "task") return Network; // subagent delegation
  if (name === "wait") return Hourglass; // deliberate yield-and-resume (#1914)
  if (name.startsWith("memory")) return Database;
  return Wrench;
}

/** Header label for a card. A `task` delegation surfaces WHICH subagent it ran
 *  (`task → researcher`), read from the call's args, so the roster is visible at a
 *  glance without expanding. The subagent type rides in `input` from the start frame,
 *  so it shows while the delegation is still running; falls back to the bare name until
 *  the args parse. A `wait` surfaces its duration the same way (`wait · ~5 minutes`,
 *  #1914) — the card is collapsed by default, so the header is where "the agent yielded
 *  deliberately" has to read at a glance. */
function cardLabel(call: ToolCall): ReactNode {
  if (call.name === "wait") {
    // A failed schedule ("Error: …" output) keeps the bare label — its card already
    // reads as an error; an ETA would claim a wait that never got scheduled.
    const failed = call.status === "error" || /^error\b/i.test((call.output ?? "").trim());
    const info = failed ? null : parseWaitInput(call.input);
    if (info) {
      return (
        <>
          wait <span className="tool-wait-eta">· ~{humanizeSeconds(info.seconds)}</span>
        </>
      );
    }
    return call.name;
  }
  if (call.name !== "task" || !call.input) return call.name;
  try {
    const args = JSON.parse(call.input) as { subagent_type?: unknown };
    const sub = args.subagent_type;
    if (typeof sub === "string" && sub) {
      return (
        <>
          task <span className="tool-subagent">→ {sub}</span>
        </>
      );
    }
  } catch {
    /* args not valid JSON yet (mid-stream) — fall back to the bare name */
  }
  return call.name;
}

/** True when this call DISPATCHES background work — `delegate_to(background=true)` or
 *  `task(run_in_background=true)` (#2896). Read from the call's args at render time (no
 *  schema change): the flag rides `input` from the start frame. These calls are receipts,
 *  not work — the tool returns "Started a background delegation…" immediately and the real
 *  result arrives later as its own report message — so ToolCalls folds them into one compact
 *  chip instead of full-height cards. A parse failure (args still streaming) reads as
 *  foreground; the call migrates into the chip once the args parse. */
function isBackgroundDispatch(call: ToolCall): boolean {
  if (call.name !== "delegate_to" && call.name !== "task") return false;
  if (!call.input) return false;
  try {
    const args = JSON.parse(call.input) as { background?: unknown; run_in_background?: unknown };
    return call.name === "delegate_to" ? args.background === true : args.run_in_background === true;
  } catch {
    return false;
  }
}

/**
 * Renders the agent's tool activity as collapsible cards inside an assistant
 * message. Each card shows the tool name, a running→done/error state pill, and
 * (when expanded) the input preview + result preview the server streamed over
 * the tool-call DataPart. The disclosure FRAME (card chrome, header, caret,
 * status glyph, duration, nesting) is the DS `ToolCard` family; the body is our
 * per-tool value renderers (`ToolValue` via `tool-renderers.tsx`).
 */
export function ToolCalls({
  calls,
  streaming = false,
  flat = false,
  spotlight = false,
  onCancelDelegation,
  onDismissToolCall,
}: {
  calls: ToolCall[];
  /** The turn is still live. Keeps the spotlight slot reserved for the whole turn so the
   *  layout doesn't bounce in the gap between one tool finishing and the next starting. */
  streaming?: boolean;
  /** Render every card plainly — no spotlight, no fold chip. For use INSIDE the WorkBlock,
   *  where the whole reason→tool timeline is already folded behind one disclosure. */
  flat?: boolean;
  /** Spotlight ONLY the most-recent tool, in a single slot with a STABLE identity — the
   *  card updates in place (name/status/output swap) as tools advance instead of remounting
   *  per tool. Without this, a rapid fan-out (e.g. task_batch's many children) strobes: each
   *  new id remounts the card, replaying its mount animation and flashing the prior output. */
  spotlight?: boolean;
  /** Abort a running top-level `task` delegation by its tool-call id (Tier 2). When
   *  omitted, no Stop affordance renders (e.g. historical/finished messages). */
  onCancelDelegation?: (id: string) => void;
  /** Dismiss a settled CANCELLED delegation card from this client's transcript (local-only —
   *  backend history is never mutated; see dismissedToolCalls.ts). ToolGroup gates the × to
   *  cancelled delegations only; when omitted, no dismiss affordance renders anywhere. */
  onDismissToolCall?: (id: string) => void;
}) {
  // Every call in the list can be dismissed away at render time (a cancelled delegation
  // ×'d out — hideDismissedToolCalls strips it and its children from the message), and an
  // empty ToolCardList would still paint its chrome/margins. Nothing to show → nothing.
  if (calls.length === 0) return null;
  // Group children (tools that ran inside a `task` subagent) under their parent.
  const childrenByParent = new Map<string, ToolCall[]>();
  const top: ToolCall[] = [];
  for (const call of calls) {
    if (call.parentId) {
      const arr = childrenByParent.get(call.parentId);
      if (arr) arr.push(call);
      else childrenByParent.set(call.parentId, [call]);
    } else {
      top.push(call);
    }
  }
  // Background dispatches (#2896) fold into their OWN chip — always, even a single one,
  // and never the spotlight slot: a turn that fires 3+ would otherwise bury its answer
  // under identical "Started a background delegation…" cards. Everything else (fg) keeps
  // the existing spotlight/fold/settled behavior.
  const bg = top.filter(isBackgroundDispatch);
  const fg = top.filter((c) => !isBackgroundDispatch(c));

  const settled = fg.filter((c) => c.status !== "running");
  const failedCount = fg.filter((c) => c.status === "error").length;

  // Only TOP-LEVEL `task` groups get the cancel callback (the Stop affordance only shows
  // for a running task); nested children and settled cards never need it. The dismiss
  // callback rides along unconditionally — ToolGroup itself gates it to cancelled
  // delegations, so it renders nothing on running/completed/failed cards.
  const group = (call: ToolCall) => (
    <ToolGroup
      key={call.id}
      call={call}
      childrenByParent={childrenByParent}
      onCancelDelegation={call.status === "running" ? onCancelDelegation : undefined}
      onDismissToolCall={onDismissToolCall}
    />
  );

  // The folded summary chip — the block's running total, with the given finished cards inside.
  //
  // KEYED, like every other slot below: the branches under this render different numbers of
  // children (a live turn has a spotlight, a settled one doesn't), so React — which matches
  // unkeyed children by POSITION — would see a chip arrive where a spotlight used to be and
  // remount it. `ToolCardSummary`'s `open` is uncontrolled, so a remount silently collapses
  // whatever the operator had expanded, at the moment their turn finished (#3390).
  const chip = (count: number, folded: ToolCall[]) => (
    <ToolCardSummary
      key="fold"
      count={count}
      label={count === 1 ? "tool" : "tools"}
      status={failedCount > 0 ? "error" : "done"}
      failedCount={failedCount || undefined}
    >
      {folded.map(group)}
    </ToolCardSummary>
  );

  // The background-dispatch chip (#2896) — the muted `.tool-bg-summary` variant: it reports
  // that jobs were FIRED, not what they produced (the results arrive later as their own
  // report messages). Expanding discloses the individual dispatch cards.
  const bgFailedCount = bg.filter((c) => c.status === "error").length;
  const bgChip =
    bg.length > 0 ? (
      <div className="tool-bg-summary" key="bg">
        <ToolCardSummary
          count={bg.length}
          label={bg.length === 1 ? "background job" : "background jobs"}
          status={bgFailedCount > 0 ? "error" : "done"}
          failedCount={bgFailedCount || undefined}
        >
          {bg.map(group)}
        </ToolCardSummary>
      </div>
    ) : null;

  // Inside the WorkBlock timeline / publish preview: just the plain cards, in order — no
  // fold, no bg chip (the whole timeline is already behind one disclosure, and the publish
  // preview renders history 1:1).
  if (flat) {
    return <ToolCardList className="tool-calls">{top.map(group)}</ToolCardList>;
  }

  // The LEAD slot: the one foreground card that renders on its own, outside any chip — the
  // most-recent tool while the turn is live (`.tool-spotlight`), and a lone tool once it
  // settles (`.tool-lead`, layout-transparent). A single, identity-STABLE slot:
  //  - live, the fixed key keeps React updating one card in place as the current tool
  //    changes, so a fast fan-out advances smoothly instead of remounting (and strobing) on
  //    every new tool id;
  //  - across the SETTLE, a single-tool turn's card is the same element at the same key in
  //    both branches, so React updates it in place instead of rebuilding it. It used to be a
  //    different tree (`key={call.id}`, no wrapper), and since the DS `ToolCard`'s `open` is
  //    uncontrolled, the rebuild collapsed whatever the operator had expanded mid-turn at the
  //    moment their turn finished — the single-tool twin of the chips' #3390.
  // Only the CLASS changes at the settle: `.tool-spotlight` stays the live-only marker that
  // e2e/toolcard.ts gates on. Background dispatches never take the slot — they go straight
  // into their chip below it.
  const lead = (call: ToolCall, live: boolean) => (
    <div className={live ? "tool-spotlight" : "tool-lead"} key="spotlight">
      <ToolGroup
        key="__spotlight__"
        call={call}
        childrenByParent={childrenByParent}
        onCancelDelegation={call.status === "running" ? onCancelDelegation : undefined}
        onDismissToolCall={onDismissToolCall}
      />
    </div>
  );

  // WorkBlock's live slot: only the most-recent tool (the timeline holds the rest).
  if (spotlight) {
    if (top.length === 0) return null;
    const current = fg[fg.length - 1];
    return (
      <ToolCardList className="tool-calls">
        {current ? lead(current, true) : null}
        {bgChip}
      </ToolCardList>
    );
  }

  // LIVE TURN: keep the MOST-RECENT foreground tool in the spotlight slot until a newer one
  // replaces it — so the slot is never empty (no blank gap between tools, or during the
  // answer tail after the last tool finishes). Everything older folds into the running-total
  // chip; a new tool crossfades into the slot and the previous one drops into the chip.
  // Background dispatches bypass the slot entirely and land in their own chip.
  if (streaming) {
    if (top.length === 0) return null;
    const current = fg[fg.length - 1];
    const folded = fg.slice(0, -1);
    return (
      <ToolCardList className="tool-calls">
        {/* Stable key: the slot updates in place as the current tool advances (no remount
            strobe — see the `spotlight` prop note) and survives the settle (see `lead`). */}
        {current ? lead(current, true) : null}
        {folded.length > 0 && chip(fg.length, folded)}
        {bgChip}
      </ToolCardList>
    );
  }

  // SETTLED (turn done for this block): a lone finished tool renders inline (no pointless
  // "1 tool" chip); a real fan-out (≥2) stays folded. The bg chip rides along either way.
  if (settled.length >= 2) {
    return (
      <ToolCardList className="tool-calls">
        {chip(settled.length, settled)}
        {bgChip}
      </ToolCardList>
    );
  }
  // A lone foreground tool keeps the LEAD slot it streamed in, so the card the operator saw
  // (and maybe expanded) while the turn ran is the card that stays — see `lead`.
  if (fg.length === 1) {
    return (
      <ToolCardList className="tool-calls">
        {lead(fg[0], false)}
        {bgChip}
      </ToolCardList>
    );
  }
  return (
    <ToolCardList className="tool-calls">
      {/* Fragment-keyed for the same reason as the chips: this slot holds an ARRAY in one
          branch and nothing in another, and an unkeyed neighbour shifting position is what
          remounts the chip beside it. */}
      <Fragment key="cards">{fg.map(group)}</Fragment>
      {bgChip}
    </ToolCardList>
  );
}

/** A tool card. For a subagent `task`, its child tool cards collapse INSIDE the card's
 *  body (revealed on expand) and the header shows a running count
 *  ("task → researcher · 3 tools"). Keeping them in the collapsible body — not the DS
 *  always-on `nested` rail — is what lets the card hold a STABLE one-row height while the
 *  subagent works, instead of growing a rail and then collapsing when it folds. */
function ToolGroup({
  call,
  childrenByParent,
  onCancelDelegation,
  onDismissToolCall,
}: {
  call: ToolCall;
  childrenByParent: Map<string, ToolCall[]>;
  onCancelDelegation?: (id: string) => void;
  onDismissToolCall?: (id: string) => void;
}) {
  const kids = childrenByParent.get(call.id);
  const nestedCards = kids?.length
    ? kids.map((kid) => (
        // Children inherit no cancel callback — they aren't independent delegations.
        <ToolGroup key={kid.id} call={kid} childrenByParent={childrenByParent} />
      ))
    : undefined;

  // Collapsed by default; expanding reveals the args/result AND the subagent's nested
  // tools (the `.pl-toolcard__children` indented rail, but here gated by the card's open
  // state instead of always-on — so the header row stays a stable height as kids stream in).
  const Icon = iconFor(call.name);
  // A coding delegate's live view (#3979) — on an `@` mention card. While the card runs it
  // renders OUTSIDE the collapsed body, under the header, so the plan and current tool are
  // visible without expanding (the whole point: the card used to be a bare spinner); once
  // the card settles it moves INTO the body, the run's final state one click away.
  const hasProgress = Boolean(call.delegateProgress && Object.keys(call.delegateProgress).length);
  const liveProgress = hasProgress && call.status === "running";
  const settledProgress =
    hasProgress && !liveProgress ? <DelegateProgressList progress={call.delegateProgress} live={false} /> : null;
  const body =
    call.input || call.output || nestedCards || settledProgress ? (
      <>
        {call.input ? (
          <ToolSection label="input" copyText={call.input}>
            <ToolValue raw={call.input} role="input" tool={call.name} />
          </ToolSection>
        ) : null}
        {call.output ? (
          <ToolSection label="result" copyText={call.output}>
            {/* `input` rides along for output renderers that need both sides — the `wait`
                waiting block derives its duration/resume-plan from the args (#1914). */}
            <ToolValue raw={call.output} role="output" tool={call.name} input={call.input} />
          </ToolSection>
        ) : null}
        {nestedCards ? <div className="pl-toolcard__children">{nestedCards}</div> : null}
        {settledProgress}
      </>
    ) : undefined;

  const openToolSettings = useUI((s) => s.openToolSettings);

  // A running subagent delegation can be aborted (Tier 2): Stop cancels just this
  // `task`, the lead keeps working. Lives in the DS ToolCard header `actions` slot
  // (a sibling of the disclosure toggle, so it doesn't expand the card).
  const stopBtn =
    onCancelDelegation && call.name === "task" && call.status === "running" ? (
      <button
        type="button"
        className="pl-iconbtn tool-cancel-btn"
        title="Stop this delegation — the agent keeps working without its result"
        aria-label="Stop this delegation"
        onClick={(e) => {
          e.stopPropagation();
          onCancelDelegation(call.id);
        }}
      >
        <Square size={11} />
        <span>Stop</span>
      </button>
    ) : null;

  // "Manage" deep-link (#1803): the moment you spot a tool you don't want, jump straight to
  // its row in Capabilities ▸ Tools to disable it or change its policy — no hunting through a
  // separate settings panel. A deep-link, not an inline toggle, so Tools settings stays the
  // single source of truth for wiring. Sits in the same header actions slot as Stop; quiet
  // until hover so the card reads as activity first, curation on intent.
  const manageBtn = (
    <button
      type="button"
      className="pl-iconbtn tool-manage-btn"
      title={`Manage “${call.name}” in Tools settings`}
      aria-label={`Manage ${call.name} in Tools settings`}
      onClick={(e) => {
        e.stopPropagation();
        openToolSettings(call.name);
      }}
    >
      <SlidersHorizontal size={11} />
    </button>
  );

  // A settled CANCELLED delegation is dead weight in the transcript — its sentinel result
  // carries nothing the operator needs later — so it alone gets a local dismiss (×) in the
  // same header actions slot. Gated by isCancelledDelegation: a normal completed call (real
  // output) or a real failure never renders this control, so their chrome is unchanged.
  const dismissBtn =
    onDismissToolCall && isCancelledDelegation(call) ? (
      <button
        type="button"
        className="pl-iconbtn tool-dismiss-btn"
        title="Dismiss this cancelled delegation — hides the card in this view only; chat history is untouched"
        aria-label="Dismiss cancelled delegation"
        onClick={(e) => {
          e.stopPropagation();
          onDismissToolCall(call.id);
        }}
      >
        <X size={11} />
      </button>
    ) : null;

  const actions = (
    <>
      {stopBtn}
      {dismissBtn}
      {manageBtn}
    </>
  );

  // A running count of the subagent's tools, in the header — so a collapsed delegation
  // reads "task → researcher · 3 tools" at a glance without expanding.
  const kidCount = kids?.length ?? 0;

  // Live elapsed, in the header, while the call is in flight. The DS renders `duration`
  // on the right once a call SETTLES; until then there is nothing to render, so a card
  // three seconds in and one fifteen minutes in look identical — and "how long has this
  // been going?" is the question behind "should I stop it?".
  //
  // It reports AGE, not liveness: the value is `now - startedAt`, so it climbs the same
  // whether the call is working or wedged. Nothing client-side can tell those apart —
  // that is the server's job (a wedged turn is failed by the stall watchdog, #2349).
  // A call whose turn PARKED on the operator (#3946 — an `ask_human` waiting on its form)
  // isn't working: no spinner, no climbing timer, just "waiting for you".
  const waiting = call.status === "running" && call.paused === true;
  const elapsedMs = useElapsed(call.status === "running" && !waiting ? call.startedAt : undefined);
  const showElapsed = elapsedMs !== undefined && elapsedMs >= SHOW_ELAPSED_AFTER_MS;

  // Context cost of the result, estimated from its size (#2282). Only on settled calls
  // that returned something substantial, so its presence alone reads as "this one was
  // expensive" — same design as the elapsed chip above.
  const costTokens = toolCostTokens(call);

  const name = (
    <>
      {cardLabel(call)}
      {kidCount > 0 ? (
        <span className="tool-nested-count">
          {" · "}
          {kidCount} {kidCount === 1 ? "tool" : "tools"}
        </span>
      ) : null}
      {showElapsed ? (
        <span className="tool-elapsed" title="How long this call has been running">
          {" · "}
          {formatElapsed(elapsedMs)}
        </span>
      ) : null}
      {waiting ? (
        <span className="tool-waiting" title="The turn is paused until you answer">
          {" · "}
          <PauseCircle size={11} aria-hidden /> waiting for you
        </span>
      ) : null}
      {costTokens !== null ? (
        <span
          className="tool-ctx-cost"
          title={
            "Estimated context cost of this result — about " +
            `${costTokens.toLocaleString()} tokens, from its size (${CHARS_PER_TOKEN} chars ≈ 1 token).\n\n` +
            "An estimate, not a measurement: it doesn't know how much of the output the " +
            "model actually reads, or whether compaction trims it first. The cost lands on " +
            "the next model call."
          }
        >
          {" · ~"}
          {tokens(costTokens)} ctx
        </span>
      ) : null}
    </>
  );

  const card = (
    <ToolCard
      name={name}
      // The DS has no paused glyph: a waiting card drops the spinner (done frame, glyph
      // hidden by .tool-paused) and says "waiting for you" in its header instead.
      status={waiting ? "done" : call.status}
      className={waiting ? "tool-paused" : undefined}
      icon={<Icon size={13} />}
      duration={call.durationMs}
      actions={actions}
    >
      {body}
    </ToolCard>
  );
  if (!liveProgress) return card;
  return (
    <>
      {card}
      <DelegateProgressList progress={call.delegateProgress} live={!waiting} />
    </>
  );
}
