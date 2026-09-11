import { useEffect } from "react";

import { chatStore, type ServerTurnControlState } from "../chat/chat-store";
import { isReattaching } from "../chat/reattach";
import {
  labelForOrigin,
  liveMessageId,
  noteTurnFinished,
  noteTurnStarted,
  rememberOrigin,
  serverTurnLabel,
} from "../chat/server-turn-store";
import { onTopic } from "../lib/events";
import {
  applyProgressFrame,
  type ChatProgressEvent,
  parseProgress,
  type ProgressFrame,
} from "./serverTurnProgress";

// Bridges the #1767 `turn.started` / `turn.finished` bus events into the server-turn store,
// so ChatSurface can show its typing indicator during a server-initiated turn (background
// push-resume, a scheduled fire, or a watch reaction). These turns run by self-POSTing into
// a session and hold the connection open for the whole turn — the browser never streams
// them, so without this the app looks hung during its longest turns.
//
// It also folds `chat.progress` frames (#2361) into a growing assistant bubble, so these
// turns are watchable while they run instead of showing a typing indicator for minutes and
// then the whole answer at once. The backend republishes tool + narration frames for
// server-fired turns only — a turn the browser streams itself is never republished, or
// every card would render twice.
//
// Display-only: the backend owns conversation history, so nothing here is fed back to the
// model. The live bubble is a preview that `chat.resumed` REPLACES with the authoritative
// final answer (ChatResumeWatch matches it by id). Mounted once, app-wide, alongside the
// other bus watchers.

export type ServerTurnControl = {
  session_id: string;
  task_id: string;
  origin: string;
  trigger: string;
  controllable: boolean;
  operator_controllable: boolean;
};

export function parseServerTurnControl(value: unknown): ServerTurnControl | null {
  if (!value || typeof value !== "object") return null;
  const data = value as Record<string, unknown>;
  const session_id = String(data.session_id ?? "");
  const task_id = String(data.task_id ?? "");
  if (!session_id || !task_id) return null;
  const operator_controllable =
    data.operator_controllable === true || (data.operator_controllable == null && data.controllable === true);
  const controllable = data.controllable === true || operator_controllable;
  return {
    session_id,
    task_id,
    origin: String(data.origin ?? ""),
    trigger: String(data.trigger ?? ""),
    controllable,
    operator_controllable,
  };
}

function controlState(control: ServerTurnControl): ServerTurnControlState {
  return {
    sessionId: control.session_id,
    taskId: control.task_id,
    origin: control.origin,
    trigger: control.trigger,
    controllable: control.controllable,
    operatorControllable: control.operator_controllable,
  };
}

function emitServerTurnControl(value: unknown) {
  const control = parseServerTurnControl(value);
  if (!control) return;
  chatStore.setServerTurnControl(controlState(control));
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent("protoagent:server-turn-control", { detail: control }));
}

/** Whether the BUS copy of a frame may land while a reattach is driving that bubble.
 *
 *  One producer per bubble: a reattach's resubscribe stream replays everything, so letting
 *  the bus write the same chunks is what doubled the text. Two frame kinds are not part of
 *  that contest and must land either way — a room reply is its own bubble, which no
 *  reattach drives, and a consumed-interjection marker is one the reattach stream NEVER
 *  places (snapshot replay deliberately skips steer markers, because a flattened artifact
 *  can't say where the boundary was). Dropping the marker for a reattached turn would leave
 *  the operator's message queued under an answer that already used it — with no second
 *  producer to fix it. Placement dedupes by id, so nothing can settle twice. */
export function busMayFold(kind: ProgressFrame["kind"], reattaching: boolean): boolean {
  return kind === "room" || kind === "steer" || !reattaching;
}

/** Fold one `chat.progress` bus event into the open session's live preview. Exported so
 *  the one-producer rule is testable against the real reattach, without mounting. */
export function foldProgressEvent(data: ChatProgressEvent): void {
  const frame = parseProgress(data);
  if (!frame) return;
  if (!busMayFold(frame.kind, isReattaching(liveMessageId(frame.taskId, frame.session)))) return;
  const target = chatStore.getSnapshot().sessions.find((s) => s.id === frame.session);
  if (!target) return; // chat not open in this window — nothing to surface here
  chatStore.updateMessages(frame.session, applyProgressFrame(target.messages, frame));
}

export function ServerTurnWatch() {
  useEffect(() => {
    const offStarted = onTopic("turn.started", (data) => {
      const session = String(data.session_id ?? "");
      const origin = String(data.origin ?? "");
      emitServerTurnControl(data.control);
      if (session) {
        // Remember the RAW origin (#3028) so the terminal `chat.resumed` can tag its settled
        // message as a server result even after `turn.finished` clears the live indicator below.
        rememberOrigin(session, origin);
        noteTurnStarted(session, labelForOrigin(origin));
      }
    });
    const offFinished = onTopic("turn.finished", (data) => {
      const session = String(data.session_id ?? "");
      if (!session) return;
      const taskId = String(data.task_id ?? "");
      // Disarm the indicator FIRST: its per-session count is what tells us whether this
      // finish was the last server turn in flight, which decides the un-addressed case.
      noteTurnFinished(session);
      if (taskId) {
        chatStore.clearServerTurnControl(session, taskId);
        return;
      }
      // An older server (or a fire that never got a task id back) can't say WHICH turn
      // ended. Two nudges can overlap on one session — the second's control frame arrives
      // while the first still runs — so clearing whatever control is there would drop the
      // LIVE turn's, and with it the operator's queued interjection. Only clear once no
      // server turn remains in flight here.
      if (serverTurnLabel(session) === null) chatStore.clearServerTurnControl(session);
    });
    const offProgress = onTopic("chat.progress", (data) => {
      emitServerTurnControl(data.control);
      foldProgressEvent(data);
    });
    return () => {
      offStarted();
      offFinished();
      offProgress();
    };
  }, []);

  return null;
}
