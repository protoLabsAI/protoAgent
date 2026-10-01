/**
 * A2A wire decoding for the console's streaming chat, split out of `lib/api.ts` (#3808):
 * the DataPart / extension-metadata decoders, SSE event parsing, and the one frame
 * dispatcher shared by the live turn, the reattach stream and durable-turn replay.
 * Pure — no fetch, no routing — and must never import `lib/api.ts`.
 */
import type {
  ComponentSpec,
  ContextWindow,
  ConsumedSteer,
  HitlPayload,
  RoomReply,
  ToolEvent,
  TurnUsage,
} from "../types";
import { delegationFromFrame } from "../delegation";

type A2APart = {
  kind?: string;
  text?: string;
  data?: unknown;
  metadata?: { mimeType?: string };
};
// A Message / Artifact carries an optional `metadata` map — since protolabs-a2a 0.3.0
// that's where the SDK extensions (cost-v1, tool-call-v1) ride, keyed by extension URI.
type A2AMessage = { role?: string; parts?: A2APart[]; metadata?: Record<string, unknown> };
type A2AArtifact = { parts?: A2APart[]; metadata?: Record<string, unknown> };
type A2AStatus = {
  state?: string;
  message?: A2AMessage;
};
export type A2AFrame = {
  jsonrpc?: string;
  id?: string;
  result?: {
    // A2A 1.0 streaming frames nest the payload under task / statusUpdate /
    // artifactUpdate; A2A 0.3 used a flat `kind`-tagged result. We read both.
    task?: {
      id?: string;
      contextId?: string;
      status?: A2AStatus;
    };
    statusUpdate?: {
      taskId?: string;
      contextId?: string;
      status?: A2AStatus;
      final?: boolean;
    };
    artifactUpdate?: {
      taskId?: string;
      contextId?: string;
      artifact?: A2AArtifact;
      append?: boolean;
      lastChunk?: boolean;
    };
    // ── A2A 0.3 (back-compat) ──
    kind?: string;
    id?: string;
    taskId?: string;
    contextId?: string;
    status?: A2AStatus;
    artifact?: A2AArtifact;
    artifacts?: A2AArtifact[];
    append?: boolean;
    lastChunk?: boolean;
    final?: boolean;
  };
  error?: {
    message?: string;
  };
};

export type DurableChatSession = {
  session_id: string;
  last_updated: string | null;
  turn_count: number;
  /** The newest turn's state — served on the `parked` index (#3957), absent otherwise. */
  last_state?: string | null;
};

export type DurableChatTurn = {
  task_id: string;
  state: string;
  last_updated: string | null;
  text: string;
  status?: A2AStatus;
  artifacts?: A2AArtifact[];
  history?: A2AMessage[];
};

/**
 * Defense-in-depth for streaming (follow-up to the subagent-stream-isolation fix #1394).
 *
 * The a2a SDK stamps EVERY frame it emits — `task`, `statusUpdate`, `artifactUpdate` — with
 * the originating `contextId`, and a single console turn streams exactly ONE context (the
 * `sessionId` it sent as the message `contextId`; the server echoes it back unchanged). So a
 * frame carrying a DIFFERENT contextId is cross-talk from a concurrent turn or a detached
 * background job and must never be rendered into this turn's message. Returns true for such a
 * foreign frame. A frame with no contextId (an older server / the A2A 0.3 flat shape that
 * omits it) is never treated as foreign — the guard degrades to a no-op rather than dropping
 * legitimate output.
 */
export function frameIsForeign(frame: A2AFrame, expectedContextId: string): boolean {
  const r = frame.result;
  if (!r) return false;
  const cid = r.task?.contextId ?? r.statusUpdate?.contextId ?? r.artifactUpdate?.contextId ?? r.contextId;
  return !!cid && cid !== expectedContextId;
}

export function textFromParts(parts?: Array<{ kind?: string; text?: string }>) {
  return (parts || [])
    .filter((part) => (part.kind === undefined || part.kind === "text") && part.text)
    .map((part) => part.text)
    .join("");
}

/** Status-message metadata key the server stamps on a task whose pause another task took
 *  over (a2a_impl/hitl_routing.py SUPERSEDED_BY). */
export const SUPERSEDED_BY_KEY = "protoagent_superseded_by";
const SUPERSEDED_TEXT = /^Continued in task (\S+?)\.?$/;

/** The task that took this one's pause over, or undefined (#3963). A plain message sent
 *  while a turn waits on the operator re-parks the pause on a NEW task, and the old one is
 *  completed with a pointer to it — as metadata, and as the "Continued in task …" text a
 *  server from before the metadata wrote alone. */
export function supersededByFromStatus(status?: A2AStatus): string | undefined {
  if (!status || !/completed/i.test(status.state ?? "")) return undefined;
  const marked = status.message?.metadata?.[SUPERSEDED_BY_KEY];
  if (typeof marked === "string" && marked) return marked;
  const match = SUPERSEDED_TEXT.exec(textFromParts(status.message?.parts).trim());
  return match?.[1];
}

/** Does this artifact-update frame APPEND to the artifact (vs REPLACE it)?
 *
 *  The A2A `append` bool has NO wire presence at its default: the SDK serializes
 *  frames with proto3 semantics, so `append=false` is OMITTED from the JSON —
 *  the key is simply absent on every replace frame, including the terminal
 *  last-chunk frame that re-sends the full canonical answer (#1709). Per the
 *  A2A spec an absent/false `append` means REPLACE, so only an explicit `true`
 *  may be read as append — `append !== false` treated the terminal replace as
 *  an append and rendered every streamed answer twice. */
export function artifactAppends(update: { append?: boolean; [key: string]: unknown }): boolean {
  return update.append === true;
}

const HITL_MIME = "application/vnd.protolabs.hitl-v1+json";
const COMPONENT_MIME = "application/vnd.protolabs.component-v1+json";
const REASONING_MIME = "application/vnd.protolabs.reasoning-v1+json";
const CONTEXT_MIME = "application/vnd.protolabs.context-v1+json";
// Authorship for an `@<name>`-addressed turn (#3042) — arrives on a WORKING frame
// BEFORE the answer artifact, so the answer can be attributed as it is drawn.
const ROOM_MIME = "application/vnd.protolabs.room-v1+json";
const STEER_CONSUMED_MIME = "application/vnd.protolabs.steer-consumed-v1+json";

// The two protolabs-a2a SDK extensions we consume ride the message/artifact METADATA
// map keyed by their extension URI (protolabs-a2a 0.3.0) — they are no longer MIME-typed
// DataParts in `parts[]`, so a generic A2A client stops rendering telemetry as content.
// The template-local extensions (hitl / component / reasoning / context) are unaffected
// and stay DataParts.
const TOOL_CALL_EXT_URI = "https://proto-labs.ai/a2a/ext/tool-call-v1";
const COST_EXT_URI = "https://proto-labs.ai/a2a/ext/cost-v1";

export type RawPart = {
  kind?: string;
  data?: unknown;
  content?: { $case?: string; value?: unknown };
  metadata?: { mimeType?: string };
};

/** A metadata map as it arrives on a Message or Artifact — extension payloads keyed by URI. */
type ExtMetadata = Record<string, unknown> | undefined;

/** Read an extension payload out of a metadata map by its extension URI. */
function extByUri(metadata: ExtMetadata, uri: string): unknown {
  const value = metadata?.[uri];
  return value && typeof value === "object" ? value : null;
}

/** Read a custom DataPart's payload iff its `metadata.mimeType` matches `mime`.
 *
 * Accepts every encoding the fleet emits: A2A 1.0 member-discriminated
 * (`content.$case === "data"`, payload under `content.value`), 1.0 flattened
 * proto-JSON (top-level `data`), and legacy 0.3 (`kind: "data"` + `data`). The
 * discriminator is always `metadata.mimeType` — `kind` is not required (1.0
 * dropped it), so this keeps matching after the a2a-sdk migration. */
function dataByMime(parts: RawPart[] | undefined, mime: string): unknown {
  const part = (parts || []).find((p) => p.metadata?.mimeType === mime);
  if (!part) return null;
  if (part.content && part.content.$case === "data") return part.content.value ?? null;
  return part.data ?? null;
}

/** Pull a structured tool event off a frame's parts and map the A2A 1.0 wire
 * payload (`{toolCallId, name, phase: "started"|"completed", args, result}`)
 * onto the frontend `ToolEvent` (`{id, name, phase: "start"|"end", input,
 * output}`).
 *
 * The field rename is load-bearing: casting the raw payload straight to
 * `ToolEvent` left `id`/`input`/`output` undefined and `phase` never `"start"`.
 * With `id` undefined, `onToolCall`'s `findIndex(c => c.id === evt.id)` matched
 * the FIRST card on every event, so all of a turn's tool calls collapsed into a
 * single ever-overwriting card — the "only one tool at a time" symptom. */
function toolEventFromMeta(metadata: ExtMetadata): ToolEvent | null {
  const d = extByUri(metadata, TOOL_CALL_EXT_URI) as
    | {
        toolCallId?: string;
        name?: string;
        phase?: string;
        args?: string;
        result?: string;
        error?: string;
        parentToolCallId?: string;
        outputChars?: number;
      }
    | null;
  if (!d) return null;
  return {
    id: d.toolCallId || "",
    name: d.name || "",
    phase: d.phase === "started" ? "start" : "end",
    input: d.args,
    // A "failed" end carries the error text in `error`; fall back to it for the body.
    output: d.result ?? d.error,
    // True pre-truncation result size (#2775) — proto-JSON round-trips numbers as
    // floats, so coerce back to an int for the chip arithmetic.
    ...(typeof d.outputChars === "number" ? { outputChars: Math.floor(d.outputChars) } : {}),
    error: d.phase === "failed" || Boolean(d.error),
    // Set only for a subagent's own tool calls → nest under the `task` card by id.
    ...(d.parentToolCallId ? { parentId: d.parentToolCallId } : {}),
  };
}

/** Pull the HITL form/question payload off an input-required frame's parts. */
/** Decode a component-v1 DataPart (ADR 0051) → a {component, props} spec, or null. */
export function componentFromParts(parts?: RawPart[]): ComponentSpec | null {
  const d = dataByMime(parts, COMPONENT_MIME) as
    | { component?: string; props?: Record<string, unknown> }
    | undefined;
  if (!d || typeof d.component !== "string") return null;
  return { component: d.component, props: (d.props as Record<string, unknown>) || {} };
}

/** The author of an `@<name>`-addressed answer, off a working frame's parts (#3042).
 *  `null` for every ordinary turn — the lead agent needs no attribution. */
export function roomReplyFromParts(parts?: RawPart[]): RoomReply | null {
  const d = dataByMime(parts, ROOM_MIME) as
    | {
        author?: string;
        addressed_to?: string;
        from?: string;
        text?: string;
        ok?: boolean;
        stopped?: string;
        summary?: string;
        background?: boolean;
        job_id?: string;
        error?: string;
        in_answer?: boolean;
        note?: boolean;
      }
    | undefined;
  if (!d) return null;
  const addressedTo = typeof d.addressed_to === "string" && d.addressed_to ? d.addressed_to : undefined;
  const author = typeof d.author === "string" && d.author ? { name: d.author } : undefined;
  // A frame is either an outgoing ask (addressed_to, no author), a reply (author), or the
  // ROOM's own note (#3449) — which has neither, and is the one shape allowed to. Without
  // one of the three there is nothing to render.
  if (!addressedTo && !author && d.note !== true) return null;
  return {
    addressedTo,
    author,
    from: typeof d.from === "string" ? d.from : "operator",
    text: typeof d.text === "string" ? d.text : "",
    ok: d.ok !== false,
    stopped: typeof d.stopped === "string" ? d.stopped : undefined,
    delegation: addressedTo ? delegationFromFrame(d) : undefined,
    // Both claims are the KEY's presence (#3449) — the server omits them rather than
    // sending false, and an older server sends nothing at all, so only an explicit
    // `true` counts. `inAnswer` on a reply: the turn's answer restates it. `note`: this
    // frame IS the part of the answer no bubble carries.
    inAnswer: d.in_answer === true,
    note: d.note === true,
  };
}


/** Decode the exact model-call boundary where queued operator input was consumed. */
export function consumedSteersFromParts(parts?: RawPart[]): ConsumedSteer[] | null {
  const d = dataByMime(parts, STEER_CONSUMED_MIME) as { items?: unknown } | null;
  if (!d || !Array.isArray(d.items)) return null;
  const items = d.items.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const row = item as { id?: unknown; text?: unknown };
    return typeof row.id === "string" && row.id && typeof row.text === "string" && row.text
      ? [{ id: row.id, text: row.text }]
      : [];
  });
  return items.length ? items : null;
}

export function hitlFromParts(parts?: RawPart[]): HitlPayload | null {
  return (dataByMime(parts, HITL_MIME) as HitlPayload) || null;
}

/** Pull a streamed reasoning ("thinking") delta off a working frame's parts. */
function reasoningFromParts(parts?: RawPart[]): string | null {
  const d = dataByMime(parts, REASONING_MIME) as { text?: string } | null;
  return d?.text || null;
}

/** Decode the terminal cost-v1 extension (A2A ext) → this turn's token usage + cost, or null.
 * Read off the artifact's METADATA keyed by the cost-v1 extension URI (protolabs-a2a 0.3.0),
 * not a DataPart. Wire shape: `{ usage: {input_tokens, output_tokens, cache_read_input_tokens,
 * cache_creation_input_tokens}, costUsd?, durationMs? }`. The snake_case `usage` fields are
 * mapped to the camelCase `TurnUsage` the console renders; totalTokens is derived. */
export function costFromMeta(metadata: ExtMetadata): TurnUsage | null {
  const d = extByUri(metadata, COST_EXT_URI) as
    | {
        usage?: {
          input_tokens?: number;
          output_tokens?: number;
          cache_read_input_tokens?: number;
          cache_creation_input_tokens?: number;
        };
        costUsd?: number;
        durationMs?: number;
      }
    | null;
  if (!d || !d.usage) return null;
  const inputTokens = Number(d.usage.input_tokens || 0);
  const outputTokens = Number(d.usage.output_tokens || 0);
  return {
    inputTokens,
    outputTokens,
    totalTokens: inputTokens + outputTokens,
    cacheReadTokens: Number(d.usage.cache_read_input_tokens || 0),
    cacheCreationTokens: Number(d.usage.cache_creation_input_tokens || 0),
    ...(typeof d.costUsd === "number" ? { costUsd: d.costUsd } : {}),
    ...(typeof d.durationMs === "number" ? { durationMs: d.durationMs } : {}),
  };
}

/** Decode the terminal context-v1 DataPart (#1372) → the turn's context-window fill +
 * compaction threshold, or null. `compactionAtTokens` / `maxTokens` are present only when the
 * server could resolve a token denominator (token-based trigger); otherwise the meter shows
 * the raw size. */
export function contextFromParts(parts?: RawPart[]): ContextWindow | null {
  const d = dataByMime(parts, CONTEXT_MIME) as
    | {
        contextTokens?: number;
        compactionAtTokens?: number;
        maxTokens?: number;
        trigger?: string;
        enabled?: boolean;
        projectedTokens?: number;
      }
    | null;
  if (!d || typeof d.contextTokens !== "number") return null;
  return {
    contextTokens: d.contextTokens,
    // Proto-JSON round-trips numbers as floats — floor back for token arithmetic.
    ...(typeof d.projectedTokens === "number" ? { projectedTokens: Math.floor(d.projectedTokens) } : {}),
    ...(typeof d.compactionAtTokens === "number" ? { compactionAtTokens: d.compactionAtTokens } : {}),
    ...(typeof d.maxTokens === "number" ? { maxTokens: d.maxTokens } : {}),
    ...(typeof d.trigger === "string" ? { trigger: d.trigger } : {}),
    ...(typeof d.enabled === "boolean" ? { enabled: d.enabled } : {}),
  };
}

/** A task's answer across its artifacts. A task resumed after a HITL pause answers in
 *  one artifact per leg (#3930) — the text before the pause, then the text after — so
 *  each artifact's text opens its own paragraph instead of running on from the last. A
 *  single-artifact task (every ordinary turn) reads exactly as before. Mirrors the
 *  server's durable-turn `text` (operator_api/chat_routes.py). */
export function joinArtifactTexts(texts: string[]): string {
  return texts.filter(Boolean).join("\n\n");
}

export function textFromTerminalTask(result: NonNullable<A2AFrame["result"]>) {
  return joinArtifactTexts((result.artifacts || []).map((artifact) => textFromParts(artifact.parts)));
}

// Parse complete SSE events (blank-line-delimited) out of a buffer, dispatching
// each frame. Returns the unconsumed remainder. Shared by the streaming +
// buffered paths so both decode frames identically.
//
// The event boundary is a blank line whose line ending VARIES: the a2a-sdk
// emits CRLF (`\r\n\r\n`); the SSE spec also allows LF (`\n\n`) or CR (`\r\r`).
// Scanning for `\n\n` only — which we used to do — never matched the CRLF
// stream, so the browser parsed zero frames and chat rendered a blank bubble
// (the agent had replied). Match any blank-line boundary, and split data lines
// on any line ending. The regex matches on the raw buffer (not a normalized
// copy), so a boundary split across two fetch chunks still reassembles correctly.
export function drainSseBuffer(buffer: string, onFrame: (frame: A2AFrame) => void): string {
  const BOUNDARY = /\r\n\r\n|\n\n|\r\r/;
  let match = BOUNDARY.exec(buffer);
  while (match) {
    const rawEvent = buffer.slice(0, match.index);
    buffer = buffer.slice(match.index + match[0].length);
    match = BOUNDARY.exec(buffer);

    const data = rawEvent
      .split(/\r\n|\r|\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim())
      .join("\n");
    if (!data) continue;
    // A malformed frame is skipped, not thrown: throwing here abandoned every valid
    // frame still in the buffer and failed the whole turn over one bad event. Only
    // the parse is guarded — an error thrown by `onFrame` still propagates.
    let frame: A2AFrame;
    try {
      frame = JSON.parse(data) as A2AFrame;
    } catch (err) {
      console.warn("[a2a] skipping malformed SSE frame", err);
      continue;
    }
    onFrame(frame);
  }
  return buffer;
}

async function consumeBuffered(
  response: Response,
  onFrame: (frame: A2AFrame) => void,
): Promise<void> {
  // Await the whole body, then parse every frame at once. Loses token-by-token
  // streaming but always renders the turn — the fallback for environments that
  // don't expose a readable fetch stream.
  const text = await response.text();
  drainSseBuffer(text.endsWith("\n\n") ? text : `${text}\n\n`, onFrame);
}

// The handler surface a streaming turn drives — shared by the LIVE stream
// (SendStreamingMessage) and the REATTACH stream (SubscribeToTask after an
// agent switch / reload, Swap & Resume S1).
export type TurnStreamHandlers = {
  signal?: AbortSignal;
  /** Fires immediately before a full Task snapshot is replayed. */
  onTaskSnapshot?: () => void;
  onTaskId?: (taskId: string) => void;
  /** The task's state as of this frame — a Task snapshot's (after it is replayed) or a
   *  status update's. Lets a reattach see a PAUSED task (input-required / auth-required)
   *  without waiting for a stream the server rightly keeps open (#3930). */
  onTaskState?: (state: string) => void;
  onStatus?: (status: string) => void;
  onText?: (text: string, append: boolean) => void;
  onReasoning?: (delta: string) => void;
  onToolCall?: (evt: ToolEvent) => void;
  onComponent?: (spec: ComponentSpec) => void;
  /** One exchange of an `@<name>`-addressed turn. A chain (#3050) sends several — each
   *  is a participant speaking, so each becomes its own authored message. */
  onRoomReply?: (reply: RoomReply) => void;
  onSteerConsumed?: (items: ConsumedSteer[]) => void;
  /** A durable rebuild only (`replaySteers`): an operator message that arrived MID-task —
   *  the answer that continued a paused task on its own id (A2A §3.4.3, #3930). The
   *  task's opening message is not one; the rebuild draws that bubble separately. */
  onContinuationMessage?: (message: { role?: string; parts?: RawPart[]; metadata?: ExtMetadata }) => void;
  onCost?: (usage: TurnUsage) => void;
  onContext?: (ctx: ContextWindow) => void;
  onInputRequired?: (payload: HitlPayload) => void;
  onFailed?: (message: string) => void;
  onDone?: () => void;
};

// Replay a Task SNAPSHOT (the first frame of tasks/resubscribe, or a GetTask
// result) into the handlers: accumulated artifact text, then the durable
// history's tool/reasoning/component frames — everything the agent did while
// nobody was subscribed. A live SendStreamingMessage's initial Task frame is
// bare (submitted; no artifacts, no history), so this is a no-op there.
function replayTaskSnapshot(
  task: NonNullable<A2AFrame["result"]>,
  handlers: TurnStreamHandlers,
  opts: { replaySteers?: boolean } = {},
): void {
  const arts = (task as { artifacts?: Array<{ parts?: RawPart[]; metadata?: ExtMetadata }> }).artifacts || [];
  const accumulated = joinArtifactTexts(arts.map((a) => textFromParts(a.parts)));
  const history = ((task as { history?: Array<{ role?: string; parts?: RawPart[]; metadata?: ExtMetadata }> }).history ||
    []) as Array<{ role?: string; parts?: RawPart[]; metadata?: ExtMetadata }>;
  let openingSeen = false;
  for (const msg of history) {
    if ((msg.role || "").includes("USER") || msg.role === "user") {
      if (opts.replaySteers && openingSeen) handlers.onContinuationMessage?.(msg);
      openingSeen = true;
      continue;
    }
    const toolEvent = toolEventFromMeta(msg.metadata);
    if (toolEvent) handlers.onToolCall?.(toolEvent);
    const reasoning = reasoningFromParts(msg.parts);
    if (reasoning) handlers.onReasoning?.(reasoning);
    const component = componentFromParts(msg.parts);
    if (component) handlers.onComponent?.(component);
    // Steer-consumed markers replay only for a transcript being REBUILT from durable
    // turns (`replaySteers`), never into a live bubble: a snapshot's artifacts flatten
    // all answer text into one accumulation, so the marker's position relative to that
    // TEXT cannot be reconstructed, and a live bubble already shows the interjection
    // where it happened. A rebuild has no interjection at all unless it replays them, so
    // it takes the position the history does give — after the work that preceded it —
    // and lands the flattened answer below (see chat/sessionHydration.ts).
    if (opts.replaySteers) {
      const consumed = consumedSteersFromParts(msg.parts);
      if (consumed) handlers.onSteerConsumed?.(consumed);
    }
  }
  for (const artifact of arts) {
    const usage = costFromMeta(artifact.metadata);
    if (usage) handlers.onCost?.(usage);
    const context = contextFromParts(artifact.parts);
    if (context) handlers.onContext?.(context);
  }
  if (accumulated) handlers.onText?.(accumulated, false);
  const state = (task.status?.state || "").toString();
  if (/input.required/i.test(state)) {
    const parts = (task.status as { message?: { parts?: RawPart[] } } | undefined)?.message?.parts;
    handlers.onInputRequired?.(hitlFromParts(parts) || { question: textFromParts(parts) });
  }
}

// One A2A frame dispatcher for every streaming consumer — the live turn, the
// reattach stream, and snapshot replays all decode frames identically.
export function makeA2ADispatcher(
  sessionId: string,
  handlers: TurnStreamHandlers,
  opts: { replaySteers?: boolean } = {},
): (frame: A2AFrame) => void {
  // The task this stream has named so far. A HITL answer that CONTINUES its parked task
  // (A2A §3.4.3, #3930) gets no Task frame — the SDK sends one only when a task is created
  // — so the id comes off the first status/artifact update instead.
  let namedTaskId = "";
  const nameTask = (id: string | undefined) => {
    if (!id || id === namedTaskId) return;
    namedTaskId = id;
    handlers.onTaskId?.(id);
  };
  return (frame: A2AFrame) => {
    if (frame.error?.message) throw new Error(frame.error.message);
    const result = frame.result;
    if (!result) return;
    // Drop any frame stamped with a different contextId than this turn's — cross-talk from
    // a concurrent turn or background job can't leak into this message (see frameIsForeign).
    if (frameIsForeign(frame, sessionId)) return;
    const task = result.task ?? (result.kind === "task" ? result : undefined);
    const statusUpdate = result.statusUpdate ?? (result.kind === "status-update" ? result : undefined);
    const artifactUpdate = result.artifactUpdate ?? (result.kind === "artifact-update" ? result : undefined);
    if (task?.id) {
      namedTaskId = task.id;
      handlers.onTaskId?.(task.id);
      // Snapshot replay covers BOTH shapes: history first (the tool/reasoning
      // frames a detached client missed), then the accumulated artifact text —
      // which for a terminal task IS the final answer. A live stream's initial
      // Task frame is bare (submitted, no artifacts/history), so it's a no-op.
      handlers.onTaskSnapshot?.();
      replayTaskSnapshot(task, handlers, opts);
      handlers.onTaskState?.((task.status?.state || "").toString());
    }
    if (statusUpdate) {
      nameTask(statusUpdate.taskId);
      const state = statusUpdate.status?.state || "";
      const parts = statusUpdate.status?.message?.parts;
      const messageText = textFromParts(parts);
      const reasoning = reasoningFromParts(parts);
      if (reasoning) handlers.onReasoning?.(reasoning);
      // A reasoning-only frame carries no status text; don't let it clobber the
      // transient status line with the bare working state.
      if (!reasoning) handlers.onStatus?.(messageText || state);
      // tool-call-v1 rides the status MESSAGE's metadata (URI-keyed), not its parts.
      const toolEvent = toolEventFromMeta(statusUpdate.status?.message?.metadata);
      if (toolEvent) handlers.onToolCall?.(toolEvent);
      const component = componentFromParts(parts);
      if (component) handlers.onComponent?.(component);
      const roomReply = roomReplyFromParts(parts);
      if (roomReply) handlers.onRoomReply?.(roomReply);
      const consumedSteers = consumedSteersFromParts(parts);
      if (consumedSteers) handlers.onSteerConsumed?.(consumedSteers);
      if (state === "input-required" || state === "TASK_STATE_INPUT_REQUIRED") {
        handlers.onInputRequired?.(hitlFromParts(parts) || { question: messageText });
      }
      if (state === "failed" || state === "TASK_STATE_FAILED") {
        handlers.onFailed?.(messageText || "the turn failed");
      }
      if (state) handlers.onTaskState?.(state);
    }
    if (artifactUpdate) {
      nameTask(artifactUpdate.taskId);
      const aParts = artifactUpdate.artifact?.parts;
      const text = textFromParts(aParts);
      if (text) handlers.onText?.(text, artifactAppends(artifactUpdate));
      // The terminal answer artifact carries cost-v1 in its URI-keyed METADATA and
      // context-v1 as a DataPart (a2a_impl executor) — surface this turn's spend and
      // its context-window fill.
      const usage = costFromMeta(artifactUpdate.artifact?.metadata);
      if (usage) handlers.onCost?.(usage);
      const ctx = contextFromParts(aParts);
      if (ctx) handlers.onContext?.(ctx);
    }
  };
}

/** Replay one row from ADR 0104's durable-turn reader through the exact same
 * dispatcher as live and reattached A2A tasks. The history hydrator creates
 * the user bubble separately because snapshot replay intentionally ignores
 * ROLE_USER frames while rebuilding the assistant response — and, unlike a live
 * replay, this one surfaces the turn's consumed interjections (`onSteerConsumed`)
 * so a rebuilt transcript can show them where the agent read them. */
export function replayDurableChatTurn(
  turn: DurableChatTurn,
  sessionId: string,
  handlers: TurnStreamHandlers = {},
): void {
  const task = {
    id: turn.task_id,
    contextId: sessionId,
    status: turn.status ?? { state: turn.state },
    artifacts: turn.artifacts ?? [],
    history: turn.history ?? [],
  };
  makeA2ADispatcher(sessionId, handlers, { replaySteers: true })({ result: { task } } as A2AFrame);
}

export async function consumeSse(
  response: Response,
  onFrame: (frame: A2AFrame) => void,
): Promise<void> {
  // WKWebView (the desktop shell) doesn't reliably expose a readable stream on a
  // fetch response — `response.body` can be null, or the reader can throw before
  // the first chunk — which left the desktop chat with NO response at all (the
  // agent replied, but the SSE never rendered). Clone up front so we can fall
  // back to a buffered read (the clone keeps its own body once we lock the
  // original via getReader()).
  let fallback: Response | null = null;
  try {
    fallback = response.clone();
  } catch {
    fallback = null;
  }

  const reader = response.body?.getReader();
  if (!reader) {
    return consumeBuffered(fallback ?? response, onFrame);
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let streamed = false;

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      streamed = true;
      buffer += decoder.decode(value, { stream: true });
      buffer = drainSseBuffer(buffer, onFrame);
    }
  } catch (err) {
    // Reader threw. If we never saw a chunk and have a clone, retry buffered;
    // otherwise a mid-stream failure is real — propagate it.
    if (streamed || !fallback) throw err;
    return consumeBuffered(fallback, onFrame);
  }

  // Reader completed but delivered nothing (WKWebView can hand back a reader
  // that immediately reports `done` without ever surfacing the buffered body) —
  // render via the buffered fallback so the turn isn't silently lost.
  if (!streamed && fallback) {
    return consumeBuffered(fallback, onFrame);
  }
}
