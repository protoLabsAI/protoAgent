// ADR 0057 — the command-palette chat. A COMPACT version of the main chat that
// renders at full fidelity (markdown + streaming tool cards + reasoning + components)
// by reusing the same renderers, and drives `api.streamChat` directly with full
// handlers. ONE preserved thread per agent (stable contextId + persisted transcript,
// see paletteChatStore) — `/clear` wipes it (transcript + server checkpoint).
import { useEffect, useRef, useState } from "react";
import { Conversation, Message, PromptInput } from "@protolabsai/ui/ai";
import { ChatMessageView } from "../chat/ChatMessageView";
import { addToolRef, appendReasoning, appendText } from "../chat/parts";
import { api } from "../lib/api";
import { chatStore, effectiveReasoningEffort } from "../chat/chat-store";
import type { ChatMessage, ToolCall, ToolEvent } from "../lib/types";
import { freshPaletteThread, loadPaletteThread, savePaletteThread } from "./paletteChatStore";
import "../chat/chat.css"; // .markdown / .tool-calls / .chat-user-text / .slash-menu styles

// Upsert a streaming tool event onto a message's toolCalls AND its ordered `parts`
// (mirrors ChatSurface's onToolCall): start → a running card (nested under its parent
// `task` — authoritative `evt.parentId`, else last-open-task), and a top-level tool
// opens/extends a `tools` part in emission order so text↔tool interleave renders live;
// end → flip the matching card to done/error and stamp elapsed.
function upsertTool(message: ChatMessage, evt: ToolEvent): ChatMessage {
  const calls = [...(message.toolCalls || [])];
  const idx = calls.findIndex((c) => c.id === evt.id);
  const now = Date.now();
  let nextParts = message.parts;
  if (evt.phase === "start") {
    const openTask = [...calls].reverse().find((c) => c.name === "task" && c.status === "running" && c.id !== evt.id);
    const parentId = evt.parentId ?? openTask?.id;
    const card: ToolCall = { id: evt.id, name: evt.name, input: evt.input, status: "running", startedAt: now, parentId };
    if (idx >= 0) calls[idx] = { ...calls[idx], ...card };
    else calls.push(card);
    // Children (parentId set) nest under their parent's card — only top-level tools get a block.
    if (parentId == null) nextParts = addToolRef(message.parts, evt.id);
  } else {
    const startedAt = idx >= 0 ? calls[idx].startedAt : undefined;
    const durationMs = startedAt !== undefined ? now - startedAt : undefined;
    const endStatus: ToolCall["status"] = evt.error ? "error" : "done";
    if (idx >= 0) {
      calls[idx] = { ...calls[idx], output: evt.output, status: endStatus, durationMs };
    } else {
      calls.push({ id: evt.id, name: evt.name, output: evt.output, status: endStatus });
      nextParts = addToolRef(message.parts, evt.id);
    }
  }
  return { ...message, toolCalls: calls, parts: nextParts };
}

// Finalize a completed turn — no tool can still be "running" (mirrors onDone).
function finalize(message: ChatMessage): ChatMessage {
  const now = Date.now();
  const toolCalls = message.toolCalls?.map((c) =>
    c.status === "running"
      ? { ...c, status: "done" as const, durationMs: c.durationMs ?? (c.startedAt !== undefined ? now - c.startedAt : undefined) }
      : c,
  );
  return { ...message, status: message.status === "error" ? "error" : "done", toolCalls };
}

// Deterministic, client-side. `/clear` wipes the thread; typed or picked from the menu.
const SLASH = [{ name: "clear", description: "Wipe this chat + its history" }];

export function PaletteChat({ agentName }: { agentName: string }) {
  const [boot] = useState(loadPaletteThread); // run once
  const [messages, setMessages] = useState<ChatMessage[]>(boot.messages);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const contextRef = useRef(boot.contextId); // stable A2A contextId (= thread_id server-side)

  // Focus the composer on open AND after each turn settles (streaming → false).
  useEffect(() => {
    if (!streaming) inputRef.current?.focus();
  }, [streaming]);
  useEffect(() => () => abortRef.current?.abort(), []);
  // Preserve the thread (debounced) — survives close/reopen and reload.
  useEffect(() => {
    savePaletteThread({ contextId: contextRef.current, messages });
  }, [messages]);

  const update = (fn: (m: ChatMessage) => ChatMessage) =>
    setMessages((ms) => {
      if (!ms.length) return ms;
      const next = ms.slice();
      next[next.length - 1] = fn(next[next.length - 1]);
      return next;
    });

  // `/clear` — wipe the server checkpoint for the current thread (no attachments on a
  // palette chat, so the full retire is harmless) + start a fresh local thread.
  const clearThread = () => {
    void api.deleteChatSession(contextRef.current, false).catch(() => {});
    contextRef.current = freshPaletteThread().contextId;
    setMessages([]);
    setDraft("");
    inputRef.current?.focus();
  };

  const send = async (raw: string) => {
    const content = raw.trim();
    if (!content || streaming) return;
    if (content === "/clear") {
      clearThread();
      return;
    }
    setMessages((ms) => [
      ...ms,
      { role: "user", content },
      { role: "assistant", content: "", status: "streaming", toolCalls: [], components: [], reasoning: "", parts: [] },
    ]);
    setDraft("");
    setStreaming(true);
    const controller = new AbortController();
    abortRef.current = controller;
    const snap = chatStore.getSnapshot();
    const sess = snap.sessions.find((s) => s.id === snap.currentSessionId);
    const model = sess?.model;
    try {
      await api.streamChat(
        content,
        contextRef.current,
        {
          signal: controller.signal,
          onText: (t, append) =>
            update((m) => ({ ...m, content: append ? m.content + t : t, parts: appendText(m.parts, t, append) })),
          onReasoning: (d) =>
            update((m) => ({ ...m, reasoning: (m.reasoning ?? "") + d, parts: appendReasoning(m.parts, d) })),
          onToolCall: (evt) => update((m) => upsertTool(m, evt)),
          onComponent: (spec) => update((m) => ({ ...m, components: [...(m.components ?? []), spec] })),
          onFailed: (detail) => update((m) => ({ ...m, content: m.content || detail, status: "error" })),
          onDone: () => update(finalize),
        },
        { model, reasoningEffort: effectiveReasoningEffort(sess) },
      );
    } catch (e) {
      if (!controller.signal.aborted) {
        update((m) => ({ ...m, content: m.content || (e as Error).message || "Chat failed.", status: "error" }));
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
      update((m) => (m.status === "streaming" ? finalize(m) : m));
    }
  };

  const stop = () => abortRef.current?.abort();
  const empty = messages.length === 0;
  // Minimal slash menu — `/clear` hint while the draft starts with "/".
  const slashMatches = draft.startsWith("/")
    ? SLASH.filter((c) => c.name.startsWith(draft.slice(1).toLowerCase()))
    : [];
  const runSlash = (name: string) => {
    if (name === "clear") clearThread();
  };

  return (
    <div className="palette-chat" style={{ display: "flex", flexDirection: "column", height: 440, minHeight: 0 }}>
      <Conversation style={{ flex: 1, minHeight: 0, padding: "8px 8px 0" }}>
        {empty ? (
          <Message role="assistant">
            <span style={{ color: "var(--pl-color-fg-muted)" }}>Ask {agentName} anything. /clear wipes this thread.</span>
          </Message>
        ) : null}
        {messages.map((m, i) => (
          // Shared renderer (ADR 0035) — the SAME message tree as the main chat (reasoning,
          // tools, content, components, the report card), so the ⌘K chat never drifts. No
          // per-message action row (transient quick-chat). Streaming is read from m.status.
          <ChatMessageView key={i} message={m} />
        ))}
      </Conversation>
      <PromptInput
        value={draft}
        onChange={setDraft}
        onSubmit={() => (streaming ? stop() : send(draft))}
        loading={streaming}
        inputRef={inputRef}
        placeholder={`Message ${agentName}…  (/clear)`}
        overlay={
          slashMatches.length ? (
            <div className="slash-menu">
              {slashMatches.map((c) => (
                <button
                  key={c.name}
                  type="button"
                  className="slash-item"
                  onMouseDown={(e) => {
                    e.preventDefault(); // keep focus; run before blur
                    runSlash(c.name);
                  }}
                >
                  <span className="slash-item__label">/{c.name}</span>
                  <span className="slash-item__desc">{c.description}</span>
                </button>
              ))}
            </div>
          ) : null
        }
      />
    </div>
  );
}
