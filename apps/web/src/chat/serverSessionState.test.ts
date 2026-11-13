// The server-authoritative session-state render (ADR 0130 D1/D5, sessionHydration.ts). The
// server owns each chat's liveness; `applyServerSessionState` makes the console follow it.
// When the index reports a session IDLE, any local belief that a turn is live there — the
// #1767 server-turn indicator, the server-turn control that locks the composer, a lingering
// "streaming" status — is dropped (bd-0ad5v). Drives the real chatStore + server-turn store.

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { DurableChatSession } from "../lib/api";
import type { ServerTurnControlState } from "./chat-store";
import { chatStore } from "./chat-store";
import { labelForOrigin, noteTurnStarted, resetServerTurns, serverTurnLabel } from "./server-turn-store";
import { applyServerSessionState } from "./sessionHydration";
import { beginLocalTurn } from "./sessionLiveness";

function indexSummary(id: string, extra: Partial<DurableChatSession> = {}): DurableChatSession {
  return { session_id: id, last_updated: null, turn_count: 1, ...extra };
}

/** Arm a session exactly as a live server turn does: the typing indicator, a controllable
 *  control frame (the composer lock / steer target), and — defensively — a streaming status. */
function armServerTurn(sessionId: string, taskId: string) {
  noteTurnStarted(sessionId, labelForOrigin("scheduler"));
  const control: ServerTurnControlState = {
    sessionId,
    taskId,
    origin: "scheduler",
    trigger: "scheduler",
    controllable: true,
    operatorControllable: true,
  };
  chatStore.setServerTurnControl(control);
  chatStore.setSessionStatus(sessionId, "streaming");
}

const snap = () => chatStore.getSnapshot();

describe("applyServerSessionState (ADR 0130 D1/D5)", () => {
  beforeEach(() => resetServerTurns());
  afterEach(() => resetServerTurns());

  it("drops the indicator, the composer lock and the streaming status when the server reports idle", () => {
    const session = chatStore.createSession();
    armServerTurn(session.id, "task-ghost");

    applyServerSessionState(indexSummary(session.id, { status: "idle", live_task_id: null }));

    expect(serverTurnLabel(session.id)).toBeNull();
    expect(snap().serverTurnControls[session.id]).toBeUndefined();
    expect(snap().sessionStatusMap[session.id]).toBe("idle");
  });

  it("leaves a running session alone — the server still names a live task", () => {
    const session = chatStore.createSession();
    armServerTurn(session.id, "task-live");

    applyServerSessionState(indexSummary(session.id, { status: "running", live_task_id: "task-live" }));

    expect(serverTurnLabel(session.id)).not.toBeNull();
    expect(snap().serverTurnControls[session.id]).toBeDefined();
    expect(snap().sessionStatusMap[session.id]).toBe("streaming");
  });

  it("does not disturb THIS window's own optimistic local turn (D1)", () => {
    const session = chatStore.createSession();
    chatStore.setSessionStatus(session.id, "streaming");
    const end = beginLocalTurn(session.id);
    try {
      // The server index momentarily reads idle before this window's send has registered.
      applyServerSessionState(indexSummary(session.id, { status: "idle", live_task_id: null }));
      expect(snap().sessionStatusMap[session.id]).toBe("streaming");
    } finally {
      end();
    }
  });

  it("ignores a pre-S2 server that sends no status (local inference preserved)", () => {
    const session = chatStore.createSession();
    armServerTurn(session.id, "task-x");

    applyServerSessionState(indexSummary(session.id)); // no status / live_task_id

    expect(serverTurnLabel(session.id)).not.toBeNull();
    expect(snap().serverTurnControls[session.id]).toBeDefined();
    expect(snap().sessionStatusMap[session.id]).toBe("streaming");
  });

  it("treats a stray live_task_id alongside idle as still-live (no clear)", () => {
    const session = chatStore.createSession();
    armServerTurn(session.id, "task-y");

    // Defensive: idle with a non-null live_task_id is contradictory — keep the live producers.
    applyServerSessionState(indexSummary(session.id, { status: "idle", live_task_id: "task-y" }));

    expect(serverTurnLabel(session.id)).not.toBeNull();
    expect(snap().serverTurnControls[session.id]).toBeDefined();
  });
});
