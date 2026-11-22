/**
 * References inside a waiting job's ask (#5676).
 *
 * An agent's ask names the thing it wants looked at by id: an artifact (`a-<ms>-<hex>`, the
 * artifact store's own id shape), a board card (`bd-…`), a GitHub issue or pull request
 * (`owner/repo#N`) or a plain URL. Read as text the operator can't open any of them, so the
 * waiting detail turns each into a link:
 *
 *  - an artifact opens in the Artifact panel on its newest version, in place;
 *  - a board card opens the board on that card (the board view reads `?card=<id>`), in a new
 *    tab so a half-written reply isn't lost to a reload;
 *  - a GitHub ref and a URL open in a new tab.
 *
 * A ref whose target isn't on this agent (the Artifact panel or the board is off) stays plain
 * text: a link that opens nothing is worse than none.
 */
import type { MouseEvent, ReactNode } from "react";
import { TextLink } from "@protolabsai/ui/primitives";

import { ARTIFACT_VIEW_KEY, openArtifactRef } from "../artifacts/artifactRef";
import { viewDeepLinkUrl } from "../lib/viewDeepLink";
import { useUI } from "../state/uiStore";

export type AskRef = { kind: "artifact" | "card" | "github" | "url"; ref: string };

/** The plugin id of the project board, whose view opens a card from `?card=<id>`. */
const BOARD_PLUGIN = "project_board";
/** The panel treats any version past the newest as "the newest" — an id alone names no version. */
const NEWEST_VERSION = Number.MAX_SAFE_INTEGER;

// One pass, in priority order: a URL first, so an id inside a URL is never split out of it.
const REF = new RegExp([
  String.raw`(https?:\/\/[^\s<>"'\`]+)`,
  String.raw`(?<![\w-])(a-\d{6,}-[0-9a-f]{4,})(?![\w-])`,
  String.raw`(?<![\w-])(bd-[a-z0-9]+(?:\.[a-z0-9]+)*)(?![\w-])`,
  String.raw`(?<![\w/.#-])([A-Za-z0-9][\w.-]*\/[A-Za-z0-9][\w.-]*#\d+)(?!\w)`,
].join("|"), "g");
const KINDS = ["url", "artifact", "card", "github"] as const;

/** `text` split into plain strings and the references found in it, in reading order. */
export function parseAskRefs(text: string): (string | AskRef)[] {
  const parts: (string | AskRef)[] = [];
  let last = 0;
  for (const match of text.matchAll(REF)) {
    const group = match.slice(1).findIndex((value) => value !== undefined);
    let ref = match[0];
    // Sentence punctuation after a URL belongs to the sentence, not the address.
    if (KINDS[group] === "url") ref = ref.replace(/[.,;:!?)\]]+$/, "");
    const start = match.index ?? 0;
    if (start > last) parts.push(text.slice(last, start));
    parts.push({ kind: KINDS[group], ref });
    last = start + ref.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

/** What a bare ref string (a task link's `ref`) points at, or null when it's none we can open. */
export function classifyRef(ref: string): AskRef | null {
  const parts = parseAskRefs(ref.trim());
  return parts.length === 1 && typeof parts[0] !== "string" ? parts[0] : null;
}

function useViewPresent(match: (key: string) => boolean): boolean {
  return useUI((s) => {
    const ro = s.railOrder;
    return [...ro.left, ...ro.right, ...ro.bottom, ...(ro.hidden ?? [])].some(match);
  });
}

/** One reference as a link that opens the item, or as plain text when it can't be opened here. */
export function AskRefLink({ item, label: linkLabel }: { item: AskRef; label?: ReactNode }) {
  const artifactPanel = useViewPresent((key) => key === ARTIFACT_VIEW_KEY);
  const board = useViewPresent((key) => key.startsWith(`plugin:${BOARD_PLUGIN}:`));
  // The plain-word label is only for a working link; unopenable, the id itself is the honest text.
  const label = linkLabel ?? item.ref;
  if (item.kind === "artifact") {
    if (!artifactPanel) return <>{item.ref}</>;
    return (
      <TextLink href="#" data-ask-ref="artifact" onClick={(event: MouseEvent) => {
        event.preventDefault();
        openArtifactRef({ id: item.ref, version: NEWEST_VERSION });
      }}>{label}</TextLink>
    );
  }
  if (item.kind === "card") {
    if (!board) return <>{item.ref}</>;
    const query = new URLSearchParams({ card: item.ref }).toString();
    return <a data-ask-ref="card" href={viewDeepLinkUrl(BOARD_PLUGIN, query)} target="_blank" rel="noreferrer">{label}</a>;
  }
  const href = item.kind === "github"
    ? `https://github.com/${item.ref.replace("#", "/issues/")}`
    : item.ref;
  return <a data-ask-ref={item.kind} href={href} target="_blank" rel="noreferrer">{label}</a>;
}

/** The ask as the operator reads it: the agent's own words, with each reference a link. */
export function AskText({ text }: { text: string }) {
  return (
    <div style={{ whiteSpace: "pre-wrap" }}>
      {parseAskRefs(text).map((part, i) => typeof part === "string" ? part : <AskRefLink key={i} item={part} />)}
    </div>
  );
}

/** The plain-word name a reference gets in the Links list, where no sentence surrounds it. */
export function refLabel(item: AskRef): string {
  if (item.kind === "artifact") return "Open the artifact";
  if (item.kind === "card") return `Board card ${item.ref}`;
  return item.ref;
}
