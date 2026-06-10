import { useToast } from "@protolabsai/ui/overlays";
import { useEffect } from "react";

import { api, authToken, currentSlug } from "../lib/api";
import { notifyIfHidden } from "../lib/notify";
import type { ChatMessage } from "../lib/types";

// Cross-agent "turn finished" notifications (ADR 0042 follow-up). Each window's SSE +
// chat stream is scoped to ITS slug, so a window has no live channel to a turn running
// on another agent. But every window on this origin can see the other slugs'
// persisted chat state (`protoagent.chat.sessions[:slug]`), and the hub proxies every
// agent's A2A — so we watch the other slugs' in-flight turns and poll their durable
// tasks (the same `tasks/get` the in-window self-heal uses; the a2a-sdk has no
// `tasks/resubscribe`, so polling is the re-attach ceiling). Two signals, deduped:
//   • poll: a watched taskId reaches a terminal state on its agent;
//   • storage event: the owning window (still open) finalized the turn itself.

const POLL_MS = 5000;
const TERMINAL = /completed|failed|canceled|cancelled/i;
const NOTIFIED_KEY = "protoagent.turnwatch.notified"; // sessionStorage — survives soft reloads

type Watch = { slug: string; taskId: string; title: string };

function notifiedSet(): Set<string> {
  try {
    return new Set(JSON.parse(sessionStorage.getItem(NOTIFIED_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function markNotified(taskId: string) {
  try {
    const s = notifiedSet();
    s.add(taskId);
    sessionStorage.setItem(NOTIFIED_KEY, JSON.stringify([...s].slice(-50)));
  } catch {
    /* best-effort */
  }
}

/** In-flight turns persisted by OTHER slugs' windows (this window's own turns stream live). */
export function scanOtherSlugs(current: string): Watch[] {
  const out: Watch[] = [];
  const seen = notifiedSet();
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i) || "";
    if (!key.startsWith("protoagent.chat.sessions")) continue;
    const slug = key === "protoagent.chat.sessions" ? "host" : key.slice("protoagent.chat.sessions:".length);
    if (slug === current) continue;
    try {
      const state = JSON.parse(localStorage.getItem(key) || "null");
      for (const s of state?.sessions ?? []) {
        const last = [...(s.messages ?? [])].reverse().find((m: ChatMessage) => m.role === "assistant");
        if (last?.status === "streaming" && last.taskId && !seen.has(last.taskId)) {
          out.push({ slug, taskId: last.taskId, title: s.title || "a chat" });
        }
      }
    } catch {
      /* skip unparseable blobs */
    }
  }
  return out;
}

/** GetTask (A2A 1.0 proto name + version header) against a SPECIFIC agent — not the
 *  window's slug — via the hub proxy. Unary result = the task flat on `result`. */
async function taskState(slug: string, taskId: string): Promise<string> {
  const path = slug === "host" ? "/a2a" : `/agents/${encodeURIComponent(slug)}/a2a`;
  const headers: Record<string, string> = { "content-type": "application/json", "A2A-Version": "1.0" };
  const t = authToken();
  if (t) headers.Authorization = `Bearer ${t}`;
  const r = await fetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify({ jsonrpc: "2.0", id: `watch-${taskId}`, method: "GetTask", params: { id: taskId } }),
  });
  if (!r.ok) return "";
  const j = await r.json().catch(() => null);
  const res = j?.result;
  const task = res?.task ?? res;
  return String(task?.status?.state ?? "");
}

export function FleetTurnWatch() {
  const toast = useToast();

  useEffect(() => {
    const current = currentSlug();
    let stopped = false;
    let names: Record<string, string> = {}; // slug(id) → display name, lazy

    async function displayName(slug: string): Promise<string> {
      if (!Object.keys(names).length) {
        try {
          names = Object.fromEntries((await api.fleet()).agents.map((a) => [a.host ? "host" : a.id, a.name]));
        } catch {
          /* non-fleet backend — slugs are fine */
        }
      }
      return names[slug] || slug;
    }

    async function announce(w: Watch, state: string) {
      if (notifiedSet().has(w.taskId)) return;
      markNotified(w.taskId);
      const name = await displayName(w.slug);
      const failed = /fail|cancel/i.test(state);
      toast({
        tone: failed ? "error" : "success",
        title: `${name} finished a turn`,
        message: failed ? `"${w.title}" ended with ${state}` : `"${w.title}" is done — switch over to read it.`,
      });
      notifyIfHidden(`${name} finished a turn`, w.title);
    }

    let prev: Watch[] = [];
    async function round() {
      if (stopped) return;
      const candidates = scanOtherSlugs(current);
      // A watched turn that VANISHED from storage was finalized by its (open) owner window.
      for (const old of prev) {
        if (!candidates.some((c) => c.taskId === old.taskId)) void announce(old, "completed");
      }
      prev = candidates;
      for (const w of candidates) {
        try {
          const state = await taskState(w.slug, w.taskId);
          if (TERMINAL.test(state)) {
            void announce(w, state);
            prev = prev.filter((p) => p.taskId !== w.taskId);
          }
        } catch {
          /* unreachable agent — keep watching */
        }
      }
    }

    const timer = setInterval(() => void round(), POLL_MS);
    const onStorage = (e: StorageEvent) => {
      if (e.key?.startsWith("protoagent.chat.sessions")) void round();
    };
    window.addEventListener("storage", onStorage);
    void round();
    return () => {
      stopped = true;
      clearInterval(timer);
      window.removeEventListener("storage", onStorage);
    };
  }, [toast]);

  return null;
}
