// Per-tool-call-id buffer for the streamed tool-argument preview (ADR 0118 D3, S3 console
// decode). The server decodes one tool's declared string argument incrementally and emits it
// as contiguous `tool-args-v1` slices (`{id, arg, offset, chunk, done}`) on WORKING frames;
// the console reassembles them per tool-call id so a later stage (S8) can render the argument
// as the model writes it. Pure — the live-turn wiring and its turn-end clear live in
// `turnReducers.ts`. LIVE-ONLY: these previews are never persisted and never rebuilt on
// hydration/reattach (the durable store drops them, and snapshot replay never decodes one).

import type { ToolArgsEvent } from "../lib/api/a2aStream";

/** One tool call's streamed-argument preview — what a consumer (S8) reads. */
export type ToolArgsBuffer = {
  /** The streamed argument's name — the tool's declared `stream_args` value (e.g. "code"). */
  arg: string;
  /** The decoded value so far, reassembled in offset order. */
  text: string;
  /** The final frame for this arg has arrived (its closing quote, or the model call ended). */
  done: boolean;
};

/** A buffer plus the bookkeeping needed to reassemble out-of-order frames. `pending` holds
 *  chunks that arrived ahead of the gap before them (offset → chunk); it is empty whenever
 *  frames arrive contiguously (the common case) and drains into `text` as each gap closes.
 *  Internal to the accumulation — consumers read the public {arg, text, done} via
 *  `toToolArgsBuffer`. */
export type ToolArgsAccum = ToolArgsBuffer & { pending: Record<number, string> };

/** The per-tool-call-id buffer map for one live turn, keyed by tool-call id. */
export type ToolArgsBuffers = Record<string, ToolArgsAccum>;

/** A fresh, empty buffer map — the state a turn starts from and resets to when it ends. */
export function emptyToolArgs(): ToolArgsBuffers {
  return {};
}

/** The public view of one tool call's preview, or undefined — with `pending` stripped. */
export function toToolArgsBuffer(buffers: ToolArgsBuffers, id: string): ToolArgsBuffer | undefined {
  const acc = buffers[id];
  return acc ? { arg: acc.arg, text: acc.text, done: acc.done } : undefined;
}

/** Number of Unicode code points in `s`. The server measures `offset`/`chunk` length in
 *  Python code points (`stream.emitted += len(chunk)`, server/turn_stream.py), so every offset
 *  comparison here must count code points too — NOT JS `.length`, which counts UTF-16 code
 *  units and so double-counts every non-BMP character (an emoji, say). Spreading a string
 *  iterates it by code point. */
function cpLength(s: string): number {
  return [...s].length;
}

/** Fold one decoded tool-args-v1 frame into its tool call's buffer, returning the next map
 *  (pure — the input map is left untouched). Appends `chunk` at `offset`: a contiguous or
 *  overlapping chunk extends `text` and the overlapping prefix is dropped, so a duplicated or
 *  re-sent frame is a no-op; a chunk that arrives ahead of a gap is stashed in `pending` and
 *  merged once the gap closes. So out-of-order and duplicated frames both converge on the
 *  correct text, and `done` latches once any frame for this arg sets it. All offset arithmetic
 *  is in code points (see `cpLength`) so a non-BMP character never shifts the reassembly. */
export function appendToolArgs(buffers: ToolArgsBuffers, evt: ToolArgsEvent): ToolArgsBuffers {
  const prev = buffers[evt.id];
  let text = prev?.text ?? "";
  let textLen = cpLength(text); // the assembled length in CODE POINTS — the offsets' unit
  const pending: Record<number, string> = { ...(prev?.pending ?? {}) };

  // Place `chunk` at `offset` if it reaches the current end; otherwise stash it (keeping the
  // longer of any chunk already stashed at that offset). `offset`/lengths are code points, so
  // slice by code point (`[...chunk]`), never by UTF-16 index. Returns whether `text` grew.
  const place = (offset: number, chunk: string): boolean => {
    const cps = [...chunk];
    if (offset + cps.length <= textLen) return false; // wholly behind the end — a duplicate
    if (offset <= textLen) {
      const tail = cps.slice(textLen - offset); // contiguous/overlapping — keep only the new tail
      text += tail.join("");
      textLen += tail.length;
      return true;
    }
    const stashed = pending[offset];
    if (stashed === undefined || cps.length > cpLength(stashed)) pending[offset] = chunk;
    return false;
  };

  place(evt.offset, evt.chunk);
  // Drain whatever the new text just made contiguous — repeat until nothing more fits.
  let grew = true;
  while (grew) {
    grew = false;
    for (const key of Object.keys(pending)) {
      const offset = Number(key);
      if (offset > textLen) continue; // still ahead of a gap
      const chunk = pending[offset];
      delete pending[offset];
      if (place(offset, chunk)) grew = true; // placed; a stale duplicate simply evaporates
    }
  }

  return {
    ...buffers,
    [evt.id]: {
      arg: prev?.arg || evt.arg, // the first frame names the arg; keep it thereafter
      text,
      done: (prev?.done ?? false) || evt.done,
      pending,
    },
  };
}
