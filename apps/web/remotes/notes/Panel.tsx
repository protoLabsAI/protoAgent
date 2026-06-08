import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { getHostBridge } from "@protoagent/plugin-ui";

// The Notes plugin view (ADR 0034 S4) — a single shared markdown note, edited here and via the
// agent's read_note/write_note/append_note tools. No tabs, no undo: debounced autosave, a saved/
// saving pill, an edit↔preview toggle, and a light poll so the panel reflects the agent's writes.
const NOTE_PATH = "/api/plugins/notes/note";
const REMARK = [remarkGfm];

function bridgeOrNull() {
  try { return getHostBridge(); } catch { return null; }
}

export default function Panel() {
  const bridge = bridgeOrNull();
  const [content, setContent] = useState("");
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [preview, setPreview] = useState(false);
  const dirty = useRef(false);
  const lastSynced = useRef("");

  const noteUrl = bridge ? bridge.apiUrl(NOTE_PATH) : NOTE_PATH;
  const authedFetch = (init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    const t = bridge?.authToken() || "";
    if (t) headers.set("Authorization", `Bearer ${t}`);
    return fetch(noteUrl, { ...init, headers });
  };

  // Load on mount.
  useEffect(() => {
    authedFetch()
      .then((r) => r.json())
      .then((d) => { setContent(d.content || ""); lastSynced.current = d.content || ""; })
      .catch(() => setStatus("error"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Debounced autosave on edit.
  useEffect(() => {
    if (!dirty.current) return;
    setStatus("saving");
    const h = window.setTimeout(async () => {
      try {
        const r = await authedFetch({
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content }),
        });
        if (!r.ok) throw new Error(String(r.status));
        lastSynced.current = content;
        dirty.current = false;
        setStatus("saved");
      } catch {
        setStatus("error");
      }
    }, 700);
    return () => window.clearTimeout(h);
  }, [content]); // eslint-disable-line react-hooks/exhaustive-deps

  // Live-refresh — adopt the agent's writes when we have no unsaved edits.
  useEffect(() => {
    const h = window.setInterval(async () => {
      if (dirty.current) return;
      try {
        const d = await authedFetch().then((r) => r.json());
        if (typeof d.content === "string" && d.content !== lastSynced.current) {
          setContent(d.content);
          lastSynced.current = d.content;
        }
      } catch { /* transient */ }
    }, 4000);
    return () => window.clearInterval(h);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const onChange = (v: string) => { dirty.current = true; setContent(v); };

  const wrap: React.CSSProperties = { display: "flex", flexDirection: "column", height: "100%", minHeight: 0 };
  const bar: React.CSSProperties = {
    display: "flex", alignItems: "center", justifyContent: "space-between",
    padding: "6px 10px", borderBottom: "1px solid var(--pl-color-border, #2a2a30)", fontSize: 12,
  };
  const editor: React.CSSProperties = {
    flex: 1, minHeight: 0, resize: "none", border: "none", outline: "none", padding: 12,
    background: "transparent", color: "var(--pl-color-fg, #ededed)",
    fontFamily: "var(--pl-font-mono, ui-monospace, monospace)", fontSize: 13, lineHeight: 1.6,
  };
  const btn: React.CSSProperties = {
    background: "transparent", border: "1px solid var(--pl-color-border, #2a2a30)",
    color: "var(--pl-color-fg-muted, #9aa0aa)", borderRadius: 6, padding: "3px 10px", cursor: "pointer", fontSize: 12,
  };

  return (
    <div style={wrap}>
      <div style={bar}>
        <span style={{ color: "var(--pl-color-fg-muted, #9aa0aa)" }}>
          {status === "saving" ? "Saving…" : status === "saved" ? "Saved ✓" : status === "error" ? "Save failed" : "Notes"}
        </span>
        <button type="button" style={btn} onClick={() => setPreview((p) => !p)}>
          {preview ? "Edit" : "Preview"}
        </button>
      </div>
      {preview ? (
        <div className="markdown" style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 12 }}>
          <ReactMarkdown remarkPlugins={REMARK}>{content || "_Empty note._"}</ReactMarkdown>
        </div>
      ) : (
        <textarea
          style={editor}
          value={content}
          onChange={(e) => onChange(e.target.value)}
          placeholder="A shared note — you and the agent both write here."
          spellCheck={false}
        />
      )}
    </div>
  );
}
