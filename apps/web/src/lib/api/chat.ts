/**
 * Chat: the turn (streaming A2A + desktop fallback), session ops (delete/compact/export/
 * publish/rewind/fork), steering, task reconcile/resume, slash commands, mentions, inbox.
 *
 * One domain slice of the console `api` object (#3822). `lib/api.ts` composes every slice
 * into the single `api` object importers, `vi.mock` and `vi.spyOn(api, …)` all use — so
 * never import `lib/api.ts` from here, and never call a sibling method via `api.`/`this.`
 * (cross-domain orchestration stays in `lib/api.ts`, where it goes through `api.`).
 */
import type {
  ChatBundleManifest,
  ChatMessage,
  HitlPayload,
  InboxItem,
  MentionTarget,
  PublishedLink,
  SlashCommand,
} from "../types";
import { notifyAuthRequired } from "../auth";
import { errMsg } from "../format";
import { authToken, apiUrl, memberPath, applyAuth } from "./routing";
import { isMemberScoped, parseErrorBody, request, requestForm } from "./http";
import {
  consumedSteersFromParts,
  consumeSse,
  drainSseBuffer,
  makeA2ADispatcher,
  textFromTerminalTask,
  type A2AFrame,
  type DurableChatSession,
  type DurableChatTurn,
  type RawPart,
  type TurnStreamHandlers,
} from "./a2aStream";
import { isDesktopWebview, tauriCore } from "./desktop";

export const chatApi = {
  // #1701 Slice 2: redeem a plugin composer-form — POST the field values back to the
  // plugin's on_submit. Returns a reply note, or the next form for a multi-step wizard.
  submitChatCommandForm(body: { callback_id: string; session_id: string; answers: Record<string, unknown> }) {
    return request<{ reply?: string | null; form?: HitlPayload; callback_id?: string }>(
      "/api/chat/commands/submit",
      { method: "POST", body },
    );
  },

  // Chat attachment — extract + TIER a dropped file (FormData: `file` + `session_id`).
  // Returns a ready-to-prepend `context` block (full text for small docs, a lede +
  // retrieval note for large docs indexed under the session) so a big doc never
  // gets dumped into the turn.
  attachToChat(form: FormData) {
    return requestForm<{
      enabled: boolean;
      mode?: "inline" | "indexed";
      name?: string;
      source_type?: string;
      chars?: number;
      chunks?: number;
      context?: string;
    }>("/api/knowledge/attach", form);
  },

  chatCommands() {
    return request<{ commands: SlashCommand[] }>("/api/chat/commands");
  },

  /** Who the operator can address with `@<name>` (#3042) — the same resolver the chat
   *  dispatcher routes with, so the popover can't offer an unreachable target. */
  chatMentions() {
    return request<{ mentions: MentionTarget[] }>("/api/chat/mentions");
  },

  inbox(floor: "now" | "next" | "later" = "later", includeDelivered = false) {
    const q = `?floor=${floor}&include_delivered=${includeDelivered}`;
    return request<{ items: InboxItem[] }>(`/api/inbox${q}`);
  },

  deliverInbox(id: number) {
    return request<{ ok: boolean; delivered: number }>(`/api/inbox/${id}/deliver`, {
      method: "POST",
      body: {},
    });
  },

  chat(message: string, sessionId: string, model?: string) {
    return request<{ response: string; messages: ChatMessage[] }>("/api/chat", {
      method: "POST",
      body: { message, session_id: sessionId, ...(model ? { model } : {}) },
    });
  },

  // Retire a chat session server-side: purge its checkpoints, optionally
  // harvesting the conversation into knowledge first and/or forgetting what it
  // already wrote to memory (the delete dialog's two opt-in switches, #3493).
  // Callers await this durable commit before dropping the local tab so a failed
  // tombstone write remains visible and retryable.
  deleteChatSession(sessionId: string, harvest = false, forget = false) {
    return request<{ deleted: boolean; harvested: boolean; forgotten?: number }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}?harvest=${harvest}${forget ? "&forget=true" : ""}`,
      { method: "DELETE" },
    );
  },

  /** Wipe durable history but keep the tab/id reusable. Unlike retirement this
   * deliberately does not tombstone the id, so its next turn can be discovered. */
  clearChatSession(sessionId: string, harvest = false, forget = false) {
    return request<{ deleted: boolean; harvested: boolean; forgotten?: number }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}?harvest=${harvest}${forget ? "&forget=true" : ""}&retire=false`,
      { method: "DELETE" },
    );
  },

  // Bounded discovery + turn reads for ADR 0104 recovery. The index carries no
  // transcript content; callers fetch turns only for sessions missing locally.
  chatSessions(limit = 50) {
    return request<{ sessions: DurableChatSession[]; reason?: string }>(
      `/api/chat/sessions?limit=${Math.max(1, Math.min(limit, 200))}`,
    );
  },

  chatSessionTurns(sessionId: string, limit = 50) {
    return request<{ turns: DurableChatTurn[]; reason?: string }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/turns?limit=${Math.max(1, Math.min(limit, 200))}`,
    );
  },

  // Compact a chat session server-side (#1527): archive the raw history into
  // searchable memory, summarize it, and rewrite the LangGraph checkpoint to
  // [summary, recent tail] so the agent keeps context at lower token cost. The
  // checkpoint is the agent's REAL context, so this must be server-side — a
  // client-only trim would leave the agent's context untouched. `refused` (never
  // lossy: nothing could be archived) means the server left the thread intact.
  compactChatSession(sessionId: string) {
    return request<{
      summary: string;
      archived_chunks: number;
      kept: number;
      removed: number;
      archived: boolean;
      refused: boolean;
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/compact`, { method: "POST", body: {} });
  },

  // Export a chat session as self-contained Markdown (#2158 P1). Read-only — never
  // touches the checkpoint (unlike compact/rewind). Secrets are scrubbed server-side
  // (graph/export_op) and the kinds found come back in `redactions` so the caller can
  // tell the operator what was removed. `title` names the document heading + file.
  exportChatSession(sessionId: string, title?: string) {
    const q = title ? `?title=${encodeURIComponent(title)}` : "";
    return request<{
      found: boolean;
      markdown: string;
      message_count: number;
      redactions: string[];
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/export${q}`, { method: "GET" });
  },

  // Build the structured chat-bundle for the pre-publish review (#2179 P2, #2682) —
  // read-only, sends nothing anywhere. Behind the `chat.publish` developer flag.
  fetchPublishPreview(sessionId: string, title?: string) {
    const q = title ? `?title=${encodeURIComponent(title)}` : "";
    return request<{
      found: boolean;
      manifest: ChatBundleManifest | null;
      message_count: number;
      redactions: string[];
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/publish/preview${q}`, { method: "GET" });
  },

  // Publish a chat thread to the hosted viewer (#2179 P2, #2683). Rebuilds the bundle
  // server-side fresh — never sends the client-side preview back up — so `published:
  // false` with `reason: "not_configured"` is the expected state until the hosted
  // service (#2685) exists and an operator points `publish.endpoint_url` at it.
  publishChatSession(sessionId: string, title?: string) {
    return request<{
      published: boolean;
      link_id?: string | null;
      public_url?: string;
      revoke_token?: string;
      expires_at?: string | null;
      redactions?: string[];
      artifact_notes?: string[];
      reason?: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/publish`, { method: "POST", body: { title } });
  },

  // Everything this instance has published (#2684) — never includes the revoke token,
  // which stays server-internal (presented to the hosted service by the revoke call
  // below, never sent back to the browser after the initial publish).
  publishedLinks() {
    return request<{ links: PublishedLink[] }>("/api/chat/publish/links");
  },
  // `ok: false` with `reason: "not_configured"` when no revoke endpoint is set — same
  // honest-state shape as publishing itself, not an error status.
  revokePublishedLink(id: string) {
    return request<{ ok: boolean; reason?: string; error?: string }>(
      `/api/chat/publish/links/${encodeURIComponent(id)}/revoke`,
      { method: "POST" },
    );
  },

  // `/btw` (#2180): ask a side question about this session's context WITHOUT changing
  // it. The server runs an incognito turn on a fresh ephemeral thread seeded with the
  // main thread's messages; the main thread's checkpoint is never written. The answer is
  // rendered as an EPHEMERAL client-side note (it never goes back to the server as a real
  // turn), so the side exchange leaves no trace in the conversation.
  asideChatSession(sessionId: string, question: string) {
    return request<{
      found: boolean;
      answer: string;
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/aside`, { method: "POST", body: { question } });
  },

  // Rewind a chat session server-side (#1535): discard every message AFTER the
  // target and rewrite the LangGraph checkpoint in place, rolling the agent's live
  // context back to that point. The checkpoint is the agent's REAL context, so this
  // must be server-side — a client-only truncate would leave the agent's memory
  // intact. Intentionally DESTRUCTIVE (no archive) but never corrupting. `content`
  // is the visible bubble's text: the console's client-side message ids never appear
  // in the checkpoint, so the server locates the message by its rendered content.
  // `before: true` = exclusive cut (#2491): the target itself is discarded too —
  // Regenerate rewinds to just before the last user message so its resend REPLACES
  // the turn instead of appending a duplicate pair.
  forkChatSession(sessionId: string, newSessionId: string, messageId: string, content?: string, occurrence?: number) {
    return request<{
      found: boolean;
      kept: number;
      discarded: number;
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/fork`, {
      method: "POST",
      body: { new_session_id: newSessionId, message_id: messageId, content, occurrence },
    });
  },
  rewindChatSession(sessionId: string, messageId: string, content?: string, occurrence?: number, before?: boolean) {
    return request<{
      found: boolean;
      kept: number;
      removed: number;
      reason: string;
      message: string;
    }>(`/api/chat/sessions/${encodeURIComponent(sessionId)}/rewind`, {
      method: "POST",
      body: { message_id: messageId, content, occurrence, before },
    });
  },

  async streamChat(
    message: string,
    sessionId: string,
    handlers: TurnStreamHandlers = {},
    opts: {
      images?: { b64: string; mime: string; name: string }[];
      model?: string;
      reasoningEffort?: string;
      bypassPermissions?: boolean;
      // Incognito thread (ADR 0069 D3b): the flag is PER MESSAGE server-side, so the
      // console stamps it on EVERY send while the thread's toggle is on — a mixed
      // thread would leak earlier incognito content into a later turn's summary.
      incognito?: boolean;
      // This message ANSWERS a pending HITL form/question/approval (#1560): the server
      // resumes the parked interrupt with it (Command(resume=…)) instead of running a
      // fresh turn. Unmarked messages sent while a form is pending are held server-side
      // until the form resolves.
      hitlResume?: boolean;
      // The PARKED task a `hitlResume` answer continues (A2A §3.4.3: the client answers
      // an input-required task by sending on the same taskId). Without it the server
      // routes the answer to the session's parked task itself (#3930).
      taskId?: string;
      // How this message showed in the transcript, when that is not simply its text. The
      // server ignores both; they ride the message into the task's durable history so a
      // chat rebuilt from it (ADR 0104) draws the same user bubble: `hidden` = none (an
      // approval/dismissal resume, a regenerate, a goal kickoff), `display` = the bubble
      // text when the sent text differs from it (attachment context prepended).
      hidden?: boolean;
      display?: string;
      // Stream to a SPECIFIC fleet member (Fleet Room DM) instead of THIS window's agent:
      // the turn runs on that member via the hub proxy (/agents/<slug>/a2a). "host" = this
      // instance. Omitted → normal chat with the focused agent (apiUrl slug-routing).
      agentSlug?: string;
    } = {},
  ) {
    // DM target (opts.agentSlug) streams to that member through the hub proxy; a normal
    // chat routes to THIS window's agent via apiUrl().
    const a2aTarget = opts.agentSlug ? memberPath(opts.agentSlug, "/a2a") : apiUrl("/a2a");
    const chatTarget = opts.agentSlug ? memberPath(opts.agentSlug, "/api/chat") : apiUrl("/api/chat");
    // One A2A SendStreamingMessage body + one frame dispatcher, shared by the desktop
    // (Tauri-relayed) and browser (fetch SSE) paths so both decode turns identically.
    const rpcId = `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const buildBody = () => ({
      jsonrpc: "2.0",
      id: rpcId,
      method: "SendStreamingMessage",
      params: {
        message: {
          role: "ROLE_USER",
          parts: [
            { text: message },
            ...(opts.images || []).map((img) => ({ raw: img.b64, mediaType: img.mime, filename: img.name })),
          ],
          messageId: rpcId,
          contextId: sessionId,
          ...(opts.hitlResume && opts.taskId ? { taskId: opts.taskId } : {}),
          // Per-turn overrides ride the A2A message metadata (server/chat.py reads them):
          // the tab's chosen model + the /effort reasoning level + incognito (ADR 0069 D3b —
          // per-message server-side, stamped on every send while the thread toggle is on).
          // `hidden` / `display` are for the durable transcript only (see opts above).
          ...((opts.model ||
            opts.reasoningEffort ||
            opts.bypassPermissions ||
            opts.incognito ||
            opts.hitlResume ||
            opts.hidden ||
            opts.display !== undefined)
            ? {
                metadata: {
                  ...(opts.model ? { model: opts.model } : {}),
                  ...(opts.reasoningEffort ? { reasoning_effort: opts.reasoningEffort } : {}),
                  ...(opts.bypassPermissions ? { bypass_permissions: true } : {}),
                  ...(opts.incognito ? { incognito: true } : {}),
                  ...(opts.hitlResume ? { hitl_resume: true } : {}),
                  ...(opts.hidden ? { hidden: true } : {}),
                  ...(opts.display !== undefined ? { display: opts.display } : {}),
                },
              }
            : {}),
        },
      },
    });
    const dispatchFrame = makeA2ADispatcher(sessionId, handlers);

    // Desktop: WKWebView can't read a streaming SSE body via fetch, so relay the /a2a
    // SSE through the Tauri shell (Rust reqwest → IPC Channel) and parse frames with the
    // SAME drainSseBuffer + dispatchFrame as the browser — real token-by-token + tool-card
    // streaming. Falls back to the non-streaming `/api/chat` path if the native command
    // is unavailable or fails, so it never regresses below the old render-once behavior.
    if (isDesktopWebview()) {
      try {
        const core = tauriCore();
        if (!core) throw new Error("Tauri core API unavailable (withGlobalTauri off?)");
        const channel = new core.Channel<string>();
        let buf = "";
        channel.onmessage = (chunk) => {
          buf += chunk;
          buf = drainSseBuffer(buf, dispatchFrame);
        };
        const tok = authToken();
        await core.invoke("chat_stream", {
          url: a2aTarget,
          body: buildBody(),
          auth: tok ? `Bearer ${tok}` : null,
          onEvent: channel,
        });
        handlers.onDone?.();
        return;
      } catch (err) {
        console.warn("[desktop] native chat stream failed; falling back to /api/chat:", err);
      }
      try {
        const res = await fetch(chatTarget, {
          method: "POST",
          headers: applyAuth(new Headers({ "Content-Type": "application/json" })),
          signal: handlers.signal,
          body: JSON.stringify({
            message,
            session_id: sessionId,
            ...(opts.model ? { model: opts.model } : {}),
            // The non-streaming fallback must carry incognito too — dropping it here
            // would silently persist a thread the operator marked private.
            ...(opts.incognito ? { incognito: true } : {}),
            // …and hitl_resume (#1560) — dropping it would make the server HOLD the
            // operator's own form answer behind the form it answers (deadlock).
            ...(opts.hitlResume ? { hitl_resume: true } : {}),
          }),
        });
        if (!res.ok) {
          const raw = await res.text().catch(() => "");
          const { detail } = parseErrorBody(raw, `${res.status} ${res.statusText}`);
          handlers.onFailed?.(detail);
          return;
        }
        const data = (await res.json()) as { response?: string };
        const reply = (data.response || "").trim();
        if (reply) handlers.onText?.(reply, false);
        else handlers.onFailed?.("the turn returned no content");
      } catch (err) {
        handlers.onFailed?.(errMsg(err));
      } finally {
        handlers.onDone?.();
      }
      return;
    }

    const response = await fetch(a2aTarget, {
      method: "POST",
      headers: applyAuth(new Headers({ "Content-Type": "application/json", "A2A-Version": "1.0" })),
      signal: handlers.signal,
      // A2A 1.0 streaming RPC `SendStreamingMessage`; body built by buildBody()
      // (shared with the desktop path) — ROLE_USER, member-discriminated parts,
      // messageId + contextId, optional image parts + per-tab model metadata.
      body: JSON.stringify(buildBody()),
    });

    if (!response.ok) {
      // token-gated chat turn (#873) — but a member-scoped 401 is the focused remote's bad
      // token, not the hub's, so don't hijack the hub AuthGate (the boot gate owns that).
      // A DM (opts.agentSlug) is member-scoped by construction.
      if (response.status === 401 && !opts.agentSlug && !isMemberScoped("/a2a")) notifyAuthRequired();
      throw new Error(`${response.status} ${response.statusText}`);
    }

    await consumeSse(response, dispatchFrame);
    // The SSE stream closing is the canonical "turn complete" signal in A2A 1.0
    // (terminal-by-state, no `final` flag) — resolve the spinner here.
    handlers.onDone?.();
  },

  cancelTask(taskId: string) {
    // A2A 1.0 (a2a-sdk 1.1): proto method name + the version header — `tasks/cancel`
    // is -32601 Method not found on the live server (same rot class as the eval
    // harness's; the mock now mirrors the 1.0 wire so this can't rot silently again).
    return request<{ result?: unknown; error?: unknown }>("/a2a", {
      method: "POST",
      headers: { "A2A-Version": "1.0" },
      body: {
        jsonrpc: "2.0",
        id: `cancel-${Date.now()}`,
        method: "CancelTask",
        params: { id: taskId },
      },
    });
  },

  // Mid-turn steering: queue a user message into a RUNNING turn (folded in at the
  // next model call by SteeringMiddleware) without stopping the stream. The client
  // `id` lets the turn-end reconcile tell consumed from arrived-too-late.
  steerChat(sessionId: string, id: string, text: string) {
    return request<{ ok: boolean; id: string | null; pending: number }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/steer`,
      { method: "POST", body: { id, text } },
    );
  },
  // Items still queued for the session — read at turn-end: anything here arrived
  // after the turn's last model call and wasn't folded in (re-send as a new turn).
  // `drained` names ids a turn actually folded in: absence from `pending` alone can't
  // tell a message the agent READ from one that never arrived (the queue is in-memory,
  // and the live boundary marker is best-effort), and the console must not guess between
  // settling a message the agent never saw and re-offering one it already used. Absent
  // from an older server, which reads as "can't say" rather than "not read".
  pendingSteer(sessionId: string) {
    return request<{ pending: { id: string; text: string }[]; drained?: string[] }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/steer`,
    );
  },
  // Cancel a still-queued steer (the ✕ on a pending bubble) before it folds into
  // the turn. `removed: false` means it was already drained — the agent will act
  // on it, so the caller settles it into the thread rather than dropping it.
  cancelSteer(sessionId: string, id: string) {
    return request<{ removed: boolean; pending: number }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/steer/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    );
  },
  serverTurnInterject(sessionId: string, taskId: string, id: string, text: string) {
    return request<{ ok: boolean; id?: string | null; pending: number; reason?: string }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/server-turns/${encodeURIComponent(taskId)}/interject`,
      { method: "POST", body: { id, text } },
    );
  },
  // Abort ONE running foreground subagent delegation (the Stop on a running `task`
  // tool card, Tier 2) — cancels just that subagent, NOT the whole turn: the lead
  // continues with a 'cancelled' result. `delegationId` is the `task` tool-call id.
  // `cancelled: false` means it already finished / wasn't running (too late).
  cancelDelegation(sessionId: string, delegationId: string) {
    return request<{ cancelled: boolean; running: number }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/delegations/${encodeURIComponent(delegationId)}/cancel`,
      { method: "POST" },
    );
  },

  // Reconcile a turn against the server's durable task (A2A GetTask). Used to
  // self-heal a chat message stuck in `streaming` after the stream was
  // interrupted (reload, network blip, a stale tab) — the server task is the
  // source of truth. Returns the normalized state + the final answer text (empty
  // until terminal).
  //
  // A2A 1.0: the method is `GetTask` (+ A2A-Version header) and the unary result
  // is the task FLAT on `result` with TASK_STATE_* states. The old `tasks/get`
  // was Method-not-found against a2a-sdk 1.1 — which made this self-heal finalize
  // a still-running turn instantly with empty state (caught live 2026-06-09).
  async getTask(taskId: string): Promise<{ state: string; text: string }> {
    const res = await request<A2AFrame>("/a2a", {
      method: "POST",
      headers: { "A2A-Version": "1.0" },
      body: { jsonrpc: "2.0", id: `get-${Date.now()}`, method: "GetTask", params: { id: taskId } },
    });
    const result = res.result;
    const task = (result?.task ?? (result?.kind === "task" ? result : result)) as
      | NonNullable<A2AFrame["result"]>
      | undefined;
    if (!task) return { state: "", text: "" };
    const state = (task.status?.state || "").toString();
    return { state, text: textFromTerminalTask(task) };
  },

  /** A turn's state plus the interjection ids its DURABLE history records as folded in.
   *
   *  The steering queue is in-memory (graph/steering.py), so "no longer queued" cannot tell
   *  a message the agent read from one a restart dropped. The executor's steer-consumed
   *  marker is written into the task's history, which survives both — so this is what lets
   *  the console settle an interjection on proof instead of inference. One GetTask, because
   *  the reconcile needs the state anyway. */
  async taskSteerState(taskId: string): Promise<{ state: string; consumed: string[] }> {
    const res = await request<A2AFrame>("/a2a", {
      method: "POST",
      headers: { "A2A-Version": "1.0" },
      body: { jsonrpc: "2.0", id: `steer-get-${Date.now()}`, method: "GetTask", params: { id: taskId } },
    });
    const result = res.result;
    const task = (result?.task ?? (result?.kind === "task" ? result : result)) as
      | NonNullable<A2AFrame["result"]>
      | undefined;
    if (!task) return { state: "", consumed: [] };
    const history = ((task as { history?: Array<{ parts?: RawPart[] }> }).history || []) as Array<{
      parts?: RawPart[];
    }>;
    return {
      state: (task.status?.state || "").toString(),
      consumed: history.flatMap((entry) => consumedSteersFromParts(entry.parts) ?? []).map((item) => item.id),
    };
  },

  // Reattach to an IN-FLIGHT turn after an agent switch / reload (Swap & Resume
  // S1): A2A `SubscribeToTask` — served by the backend and forwarded by the
  // fleet proxy all along; the console just never called it. The server replays
  // a Task snapshot first (whose durable history carries everything emitted
  // while nobody was subscribed — replayed via replayTaskSnapshot), then the
  // same live frames SendStreamingMessage emits. Stream close = turn complete — but a
  // PAUSED task (input-required) keeps the stream open until the operator answers
  // (A2A §3.1.6: only a terminal state ends it), so a caller must read the paused state
  // off the frames (`onTaskState`), not wait for the close (#3930).
  // A TERMINAL task is rejected by the server (UnsupportedOperation) — callers
  // catch and fall back to replayTask() below.
  async resumeTask(taskId: string, sessionId: string, handlers: TurnStreamHandlers = {}) {
    const dispatchFrame = makeA2ADispatcher(sessionId, handlers);
    const body = {
      jsonrpc: "2.0",
      id: `resub-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      method: "SubscribeToTask",
      params: { id: taskId },
    };
    // Desktop: same WKWebView limitation as streamChat — relay the SSE through
    // the Tauri shell; any A2A streaming body rides the same command.
    if (isDesktopWebview()) {
      const core = tauriCore();
      if (!core) throw new Error("Tauri core API unavailable");
      const channel = new core.Channel<string>();
      let buf = "";
      channel.onmessage = (chunk) => {
        buf += chunk;
        buf = drainSseBuffer(buf, dispatchFrame);
      };
      const tok = authToken();
      await core.invoke("chat_stream", {
        url: apiUrl("/a2a"),
        body,
        auth: tok ? `Bearer ${tok}` : null,
        onEvent: channel,
      });
      handlers.onDone?.();
      return;
    }
    const response = await fetch(apiUrl("/a2a"), {
      method: "POST",
      headers: applyAuth(new Headers({ "Content-Type": "application/json", "A2A-Version": "1.0" })),
      signal: handlers.signal,
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    // A JSON-RPC rejection (e.g. resubscribing a TERMINAL task) comes back as a
    // plain JSON body, not an SSE stream — surfacing it as a throw is what routes
    // the caller onto the snapshot-replay fallback.
    if (!(response.headers.get("content-type") || "").includes("text/event-stream")) {
      const payload = (await response.json().catch(() => null)) as { error?: { message?: string } } | null;
      throw new Error(payload?.error?.message || "resubscribe rejected");
    }
    await consumeSse(response, dispatchFrame);
    handlers.onDone?.();
  },

  // Fetch the durable task and replay its snapshot (accumulated text + history
  // tool/reasoning frames) through the SAME dispatcher the streams use — the
  // catch-up path when the turn already ended while nobody was watching.
  // Returns the task state ("" when the task is gone).
  async replayTask(taskId: string, sessionId: string, handlers: TurnStreamHandlers = {}): Promise<string> {
    const res = await request<A2AFrame>("/a2a", {
      method: "POST",
      headers: { "A2A-Version": "1.0" },
      body: { jsonrpc: "2.0", id: `get-${Date.now()}`, method: "GetTask", params: { id: taskId } },
    });
    const result = res.result;
    if (!result) return "";
    const task = (result.task ?? (result.kind === "task" ? result : result)) as NonNullable<A2AFrame["result"]>;
    if (!task?.status) return "";
    const dispatch = makeA2ADispatcher(sessionId, handlers);
    // GetTask results aren't context-stamped frames — wrap as a task frame; the
    // dispatcher's foreign-frame guard passes frames without a contextId.
    dispatch({ result: { task } } as A2AFrame);
    return (task.status?.state || "").toString();
  },
};
