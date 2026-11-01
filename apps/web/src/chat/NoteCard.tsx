import { Card } from "@protolabsai/ui/primitives";
import { CircleAlert, CircleCheck, Info, TriangleAlert, type LucideIcon } from "lucide-react";

import type { ChatMessage, SystemNoteTone } from "../lib/types";
import { Markdown } from "./LazyMarkdown";

// A local SYSTEM NOTE (noteToThread, a background-job one-liner, an export/publish
// confirmation) rendered as a card in the thread: the same Card shell as /usage and the
// HITL response card, with a tone icon + title header over the markdown body. It replaced
// the toned left-accent banner, which read like quoted text instead of a status the
// console posted.

const TONE_ICON: Record<SystemNoteTone, LucideIcon> = {
  info: Info,
  success: CircleCheck,
  warning: TriangleAlert,
  danger: CircleAlert,
};

const TONE_TITLE: Record<SystemNoteTone, string> = {
  info: "Note",
  success: "Done",
  warning: "Heads up",
  danger: "Something went wrong",
};

// A leading bold lede, the house style for a note's headline: "**Goal set.** detail",
// "**Watch set.** …", "**↪ Aside:** question". Group 1 is the lede without its closing
// punctuation; the rest of the content is the body.
const LEDE = /^\*\*([^*\n]{1,80}?)[.:]?\*\*(?:\s+|$)/;

/** Whether a note opens with its own bold lede (which then becomes the card title). */
export function hasNoteLede(content: string): boolean {
  return LEDE.test(content);
}

/** The card's title and body for a note. An explicit `noteTitle` wins; otherwise a leading
 *  bold lede becomes the title (and leaves the body); otherwise the tone's label. Pure. */
export function noteCardParts(message: Pick<ChatMessage, "content" | "noteTone" | "noteTitle">): {
  title: string;
  body: string;
} {
  const content = message.content ?? "";
  const explicit = message.noteTitle?.trim();
  if (explicit) return { title: explicit, body: content };
  const lede = LEDE.exec(content);
  if (lede) return { title: lede[1].trim(), body: content.slice(lede[0].length) };
  return { title: TONE_TITLE[message.noteTone ?? "info"], body: content };
}

export function NoteCard({ message }: { message: Pick<ChatMessage, "content" | "noteTone" | "noteTitle"> }) {
  const tone = message.noteTone ?? "info";
  const Icon = TONE_ICON[tone];
  const { title, body } = noteCardParts(message);
  return (
    <Card className={`note-card note-card--${tone}`} data-testid="note-card" role="group" aria-label={title}>
      <header className="note-card__header">
        <Icon size={15} aria-hidden className="note-card__icon" />
        <span className="note-card__title" title={title}>
          {title}
        </span>
      </header>
      {body.trim() ? (
        <div className="note-card__body">
          <Markdown>{body}</Markdown>
        </div>
      ) : null}
    </Card>
  );
}
