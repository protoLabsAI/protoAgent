// ADR 0057 — the command-palette chat. A COMPACT version of the main chat that
// renders at full fidelity (markdown + streaming tool cards + reasoning + components)
// by reusing the same renderers, and drives `api.streamChat` directly with full
// handlers. Store-free: a single local message accumulator (no chat-store / sessions /
// steering / HITL — those are ChatSurface-specific). An ephemeral context per open.
import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Conversation, Message, PromptInput, Reasoning } from "@protolabsai/ui/ai";
import { Markdown } from "../chat/LazyMarkdown";
import { ToolCalls } from "../chat/ToolCalls";
import { ChatComponent } from "../chat/ChatComponent";
import { api } from "../lib/api";
import { chatStore } from "../chat/chat-store";
import type { ChatMessage, ToolCall, ToolEvent } from "../lib/types";
import "../chat/chat.css"; // .markdown / .tool-calls / .chat-user-text styles

// Upsert a streaming tool event onto a message's toolCalls (mirrors ChatSurface's
// onToolCall): start → a running card (nested under the last open `task`); end → flip
// the matching card to done/error and stamp elapsed.
function upsertTool(message: ChatMessage, evt: ToolEvent): ChatMessage {
  const calls = [...(message.toolCalls || [])];
  const idx = calls.findIndex((c) => c.id === evt.id);
  const now = Date.now();
  if (evt.phase === "start") {
    const openTask = [...calls].reverse().find((c) => c.name === "task" && c.status === "running" && c.id !== evt.id);
    const card: ToolCall = { id: evt.id, name: evt.name, input: evt.input, status: "running", startedAt: now, parentId: openTask?.id };
    if (idx >= 0) calls[idx] = { ...calls[idx], ...card };
    else calls.push(card);
  } else {
    const startedAt = idx >= 0 ? calls[idx].startedAt : undefined;
    const durationMs = startedAt !== undefined ? now - startedAt : undefined;
    const endStatus: ToolCall["status"] = evt.error ? "error" : "done";
    if (idx >= 0) calls[idx] = { ...calls[idx], output: evt.output, status: endStatus, durationMs };
    else calls.push({ id: evt.id, name: evt.name, output: evt.output, status: endStatus });
  }
  return { ...message, toolCalls: calls };
}

// Finalize a completed turn — no tool can still be "running" (a tool_end racing the
// terminal `done` would otherwise spin forever). Mirrors ChatSurface's onDone.
function finalize(message: ChatMessage): ChatMessage {
  const now = Date.now();
  const toolCalls = message.toolCalls?.map((c) =>
    c.status === "running"
      ? { ...c, status: "done" as const, durationMs: c.durationMs ?? (c.startedAt !== undefined ? now - c.startedAt : undefined) }
      : c,
  );
  return { ...message, status: message.status === "error" ? "error" : "done", toolCalls };
}

export function PaletteChat({ agentName }: { agentName: string }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const sessionRef = useRef("");

  // Focus the composer on open AND after each turn settles (streaming → false), so you
  // never have to click/tab back. (The effect runs once the field is re-enabled.)
  useEffect(() => {
    if (!streaming) inputRef.current?.focus();
  }, [streaming]);
  useEffect(() => () => abortRef.current?.abort(), []);

  // Mutate the in-progress (last) assistant message.
  const update = (fn: (m: ChatMessage) => ChatMessage) =>
    setMessages((ms) => {
      if (!ms.length) return ms;
      const next = ms.slice();
      next[next.length - 1] = fn(next[next.length - 1]);
      return next;
    });

  const send = async (raw: string) => {
    const content = raw.trim();
    if (!content || streaming) return;
    // Fresh ephemeral context on the first turn of this open chat.
    if (messages.every((m) => m.role !== "user")) {
      sessionRef.current = `palette-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    }
    setMessages((ms) => [
      ...ms,
      { role: "user", content },
      { role: "assistant", content: "", status: "streaming", toolCalls: [], components: [], reasoning: "" },
    ]);
    setDraft("");
    setStreaming(true);
    const controller = new AbortController();
    abortRef.current = controller;
    // Inherit the active tab's model so the palette talks to the same model.
    const snap = chatStore.getSnapshot();
    const model = snap.sessions.find((s) => s.id === snap.currentSessionId)?.model;
    try {
      await api.streamChat(
        content,
        sessionRef.current,
        {
          signal: controller.signal,
          onText: (t, append) => update((m) => ({ ...m, content: append ? m.content + t : t })),
          onReasoning: (d) => update((m) => ({ ...m, reasoning: (m.reasoning ?? "") + d })),
          onToolCall: (evt) => update((m) => upsertTool(m, evt)),
          onComponent: (spec) => update((m) => ({ ...m, components: [...(m.components ?? []), spec] })),
          onFailed: (detail) => update((m) => ({ ...m, content: m.content || `⚠️ ${detail}`, status: "error" })),
          onDone: () => update(finalize),
        },
        { model },
      );
    } catch (e) {
      if (!controller.signal.aborted) {
        update((m) => ({ ...m, content: m.content || `⚠️ ${(e as Error).message || "Chat failed."}`, status: "error" }));
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
      update((m) => (m.status === "streaming" ? finalize(m) : m));
    }
  };

  const stop = () => abortRef.current?.abort();
  const empty = messages.length === 0;
  const last = messages.length - 1;

  return (
    <div className="palette-chat" style={{ display: "flex", flexDirection: "column", height: 440, minHeight: 0 }}>
      <Conversation style={{ flex: 1, minHeight: 0, padding: "8px 8px 0" }}>
        {empty ? (
          <Message role="assistant">
            <span style={{ color: "var(--pl-color-fg-muted)" }}>Ask {agentName} anything — a quick scratch chat.</span>
          </Message>
        ) : null}
        {messages.map((m, i) => {
          const isStreaming = m.status === "streaming" && i === last;
          if (m.role === "user") {
            return (
              <Message key={i} role="user">
                <span className="chat-user-text">{m.content}</span>
              </Message>
            );
          }
          return (
            <Message key={i} role={m.role} streaming={isStreaming}>
              {m.reasoning ? <Reasoning streaming={isStreaming && !m.content}>{m.reasoning}</Reasoning> : null}
              {m.toolCalls && m.toolCalls.length ? <ToolCalls calls={m.toolCalls} /> : null}
              {m.content ? (
                <Markdown>{m.content}</Markdown>
              ) : isStreaming && !m.toolCalls?.length && !m.reasoning ? (
                <Loader2 className="spin" size={16} />
              ) : null}
              {m.components?.map((s, j) => <ChatComponent key={j} spec={s} />)}
            </Message>
          );
        })}
      </Conversation>
      <PromptInput
        value={draft}
        onChange={setDraft}
        onSubmit={() => (streaming ? stop() : send(draft))}
        loading={streaming}
        inputRef={inputRef}
        placeholder={`Message ${agentName}…`}
      />
    </div>
  );
}
