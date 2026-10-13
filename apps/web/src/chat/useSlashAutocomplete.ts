import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type React from "react";
import { useQuery } from "@tanstack/react-query";

import { registeredSlashCommands, slashTokenAt } from "../ext/slashRegistry";
import { useFlagPredicate } from "../flags/flags";
import { chatCommandsQuery, chatMentionsQuery } from "../lib/queries";
import type { SlashCommand } from "../lib/types";
import { sessionCast, type ChatSession } from "./chat-store";
import { mentionTokenAt } from "./mentionToken";

// The token the caret currently sits in, and which popover list it fills.
export type SlashTokenCtx = { query: string; start: number; end: number; sigil: "/" | "@" };

export type UseSlashAutocompleteOptions = {
  // The composer textarea (the DS PromptInput's inputRef). The hook binds its caret
  // listeners to it and re-parses the token from its live value + caret.
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  // The slot's session — the `@` list ranks who has SPOKEN in it first (#3049).
  session: Pick<ChatSession, "messages"> | null | undefined;
  draft: string;
  setDraft: React.Dispatch<React.SetStateAction<string>>;
  // The slot's client-slash dispatcher (ADR 0061). Stays in the slot because it also
  // feeds send() and registerSlashDispatcher; only called from event handlers here.
  runClientSlash: (raw: string) => boolean;
};

// Slash-command + @-mention autocomplete for the chat composer (#3850, extracted from
// ChatSessionSlot): the token state, the filtered match list, ↑/↓/Enter/Tab/Escape
// keyboard selection, and completion. Behaviour-preserving move — same state, same
// effects with the same dependency arrays; the handlers are per-render closures exactly
// as they were inline, so completion reads the render's `draft` and `runClientSlash`.
export function useSlashAutocomplete({
  textareaRef,
  session,
  draft,
  setDraft,
  runClientSlash,
}: UseSlashAutocompleteOptions) {
  // Slash-command autocomplete. The dropdown is active while typing a "/name" token
  // (before a space). Commands the SERVER handles (e.g. /goal, plugin commands, user-facing
  // skills) come from a shared QUERY, not a per-slot fetch: the list is identical for every
  // open chat tab, so one key means one fetch shared by every composer — and by the ⌘K
  // palette, which lists the same commands.
  const commandsQ = useQuery(chatCommandsQuery());
  const commands = useMemo(() => commandsQ.data?.commands ?? [], [commandsQ.data]);
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);
  // The "/name" token the caret currently sits in ({query, start}), or null. Recomputed
  // from the LIVE textarea caret (not just the draft) so the popover triggers MID-INPUT —
  // typing "/" at any cursor position opens it, not only when "/" is char 0 (#1530).
  // One token context for BOTH sigils (#3042). A draft can't start with `/` and `@` at
  // once, so `/` commands and `@` participants share one popover, one keyboard nav and
  // one completion path — the sigil only decides which list fills it.
  const [slashCtx, setSlashCtx] = useState<SlashTokenCtx | null>(null);
  // The `@`-addressable roster (#3042). A QUERY, not a one-shot fetch: its key is
  // prefixed under `delegates`, so adding or editing a delegate in Settings invalidates
  // it and the popover updates without a page reload.
  const mentionsQ = useQuery(chatMentionsQuery());
  const mentions: SlashCommand[] = useMemo(
    () =>
      (mentionsQ.data?.mentions ?? []).map((m) => ({
        name: m.name,
        kind: m.kind,
        description: m.description,
        usage: m.usage,
      })),
    [mentionsQ.data],
  );
  // Keeps the keyboard-selected item scrolled into view during ↑/↓ nav (#1528).
  const activeSlashRef = useRef<HTMLButtonElement | null>(null);

  // Re-parse the slash token from the textarea's current value + caret. Called on input,
  // on caret moves (native keyup/click/select/focus listeners below), and after any
  // programmatic caret change — so the popover state tracks the caret wherever it is.
  const refreshSlash = useCallback(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    const caret = ta.selectionStart ?? ta.value.length;
    const slash = slashTokenAt(ta.value, caret);
    if (slash) return setSlashCtx({ ...slash, sigil: "/" });
    const mention = mentionTokenAt(ta.value, caret);
    setSlashCtx(mention ? { ...mention, sigil: "@" } : null);
  }, []);

  // Caret moves that don't fire onChange (arrow keys, clicks, selection, focus) still need
  // to re-evaluate the popover so "/" mid-input opens/closes as the caret enters/leaves a token.
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.addEventListener("keyup", refreshSlash);
    ta.addEventListener("click", refreshSlash);
    ta.addEventListener("select", refreshSlash);
    ta.addEventListener("focus", refreshSlash);
    return () => {
      ta.removeEventListener("keyup", refreshSlash);
      ta.removeEventListener("click", refreshSlash);
      ta.removeEventListener("select", refreshSlash);
      ta.removeEventListener("focus", refreshSlash);
    };
  }, [refreshSlash]);

  const slashQuery = slashDismissed ? null : slashCtx?.query ?? null;

  // Developer-flag gate (ADR 0068): a registered command tagged with `flag:` is listed
  // and dispatched only while its flag resolves ON — flag-off, it's as if unregistered.
  const flagOn = useFlagPredicate();

  const slashSigil = slashDismissed ? null : slashCtx?.sigil ?? null;

  const slashMatches = useMemo(() => {
    if (slashQuery === null) return [];
    const q = slashQuery.toLowerCase();
    if (slashSigil === "@") {
      // Participants, not commands: no client registry, no flag gate, no dedup — the
      // server resolver already returned exactly the addressable set.
      const matched = mentions.filter(
        (m) => !q || m.name.toLowerCase().includes(q) || m.description.toLowerCase().includes(q),
      );
      // Who has SPOKEN in this chat comes first (#3049) — derived from the transcript,
      // so it can never disagree with what happened. Ordering, not filtering:
      // `@somebody-else` still routes; refusing an address would be a surprise, not a
      // safeguard.
      const roster = sessionCast(session);
      if (!roster.length) return matched;
      const rank = (name: string) => {
        const at = roster.indexOf(name);
        return at === -1 ? roster.length : at;
      };
      return [...matched].sort((a, b) => rank(a.name) - rank(b.name));
    }
    // Client-side commands (ADR 0061) surface first, then server skills. The client set
    // comes from the slash-command registry — core (/new, /clear, /effort) AND any fork-
    // registered commands — so neither is hardcoded here.
    const all: SlashCommand[] = [
      ...registeredSlashCommands()
        .filter((c) => !c.flag || flagOn(c.flag))
        .map((c) => ({ name: c.name, description: c.description, usage: c.usage })),
      ...commands,
    ];
    // Dedup by token: a command that exists BOTH as a client command and a server skill
    // (e.g. /goal, /clear) must appear once — the client entry (listed first) wins.
    const seen = new Set<string>();
    const unique = all.filter((c) => {
      const n = c.name.toLowerCase();
      if (seen.has(n)) return false;
      seen.add(n);
      return true;
    });
    return unique.filter(
      (c) => !q || c.name.toLowerCase().includes(q) || c.description.toLowerCase().includes(q),
    );
  }, [slashQuery, slashSigil, commands, mentions, flagOn, session?.messages]);

  const slashActive = slashMatches.length > 0;
  const slashSel = slashActive ? Math.min(slashIndex, slashMatches.length - 1) : 0;

  // Auto-scroll the keyboard-selected item into view during ↑/↓ nav so it never hides
  // below the popover's scroll edge (standard listbox behavior, #1528).
  useEffect(() => {
    if (slashActive) activeSlashRef.current?.scrollIntoView({ block: "nearest" });
  }, [slashSel, slashActive]);

  function completeCommand(cmd: SlashCommand) {
    // Replace ONLY the "/name" token the caret is in — surrounding text is preserved so a
    // command can be completed at the start, middle, or end of the draft (#1530). Fall back
    // to the whole draft if the token is somehow unknown (defensive).
    const token = slashCtx;
    const start = token ? token.start : 0;
    const end = token ? token.end : draft.length;
    // Place the caret + re-sync the popover after React commits the new value.
    const settleCaret = (pos: number) => {
      requestAnimationFrame(() => {
        const ta = textareaRef.current;
        if (!ta) return;
        // A form the command opened (e.g. /model's picker — possibly ASYNC, after a
        // schema fetch) owns focus on appear (#1978) — don't yank it back. Checked at
        // fire time against the DOM: state/refs can't see an openForm that hasn't
        // happened yet. Either race order converges on the form keeping focus.
        if (document.activeElement?.closest(".hitl-float")) return;
        ta.focus();
        ta.selectionStart = ta.selectionEnd = pos;
        refreshSlash();
      });
    };
    // A client command runs on pick — drop just its token from the draft (keeping any
    // surrounding text); a server skill inserts "/name " to edit + send.
    const sigil = token?.sigil ?? "/";
    if (sigil === "/" && runClientSlash(cmd.name)) {
      setDraft(draft.slice(0, start) + draft.slice(end));
      setSlashIndex(0);
      setSlashDismissed(true);
      setSlashCtx(null);
      settleCaret(start);
      return;
    }
    const insert = `${sigil}${cmd.name} `;
    setDraft(draft.slice(0, start) + insert + draft.slice(end));
    setSlashIndex(0);
    setSlashDismissed(true); // a space follows, so it would close anyway
    setSlashCtx(null);
    settleCaret(start + insert.length);
  }

  // The popover's slice of the composer keydown (runs first in onComposerKeyDown, ahead
  // of the DS PromptInput's Enter-to-submit). Slash-menu nav wins while open: returns
  // true when it took the key (the caller returns), false to let the key fall through.
  function onSlashKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>): boolean {
    if (!slashActive) return false;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setSlashIndex((i) => (i + 1) % slashMatches.length);
      return true;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setSlashIndex((i) => (i - 1 + slashMatches.length) % slashMatches.length);
      return true;
    }
    if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      completeCommand(slashMatches[slashSel]);
      return true;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setSlashDismissed(true);
      return true;
    }
    return false;
  }

  return {
    commands,
    flagOn,
    slashMatches,
    slashActive,
    slashSel,
    slashSigil,
    activeSlashRef,
    refreshSlash,
    setSlashIndex,
    setSlashDismissed,
    setSlashCtx,
    completeCommand,
    onSlashKeyDown,
  };
}
