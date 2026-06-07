import { BookMarked, Pin, RefreshCw, Sparkles, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { ConfirmDialog } from "../app/ConfirmDialog";
import { api } from "../lib/api";
import type { Playbook } from "../lib/types";

// Playbooks surface (ADR 0009) — browse + manage the procedural-memory skill
// index (skills.db) the operator was otherwise blind to. "Playbooks" is the
// operator-facing name for skill-v1 artifacts: disk = pinned SKILL.md,
// emitted = agent-learned (curated/decaying). Functional-first: list + search +
// delete-with-confirm. Confidence tuning / curator audit are follow-ups.

function ago(iso: string | null): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function PlaybooksSurface({ onError }: { onError: (message: string) => void }) {
  const [playbooks, setPlaybooks] = useState<Playbook[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [pending, setPending] = useState<Playbook | null>(null);

  async function load() {
    setLoading(true);
    try {
      const r = await api.playbooks();
      setEnabled(r.enabled);
      setPlaybooks(r.playbooks || []);
      onError("");
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return playbooks;
    return playbooks.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.description.toLowerCase().includes(q) ||
        p.tools_used.some((t) => t.toLowerCase().includes(q)),
    );
  }, [playbooks, query]);

  const pinned = filtered.filter((p) => p.source === "disk").length;
  const learned = filtered.length - pinned;

  async function confirmDelete() {
    if (!pending) return;
    const id = pending.id;
    setPending(null);
    try {
      const r = await api.deletePlaybook(id);
      if (!r.deleted) {
        onError(r.error || "delete failed");
        return;
      }
      setPlaybooks((ps) => ps.filter((p) => p.id !== id));
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section className="panel stage-panel" data-testid="playbooks-surface">
      <div className="panel-header">
        <div>
          <h1>Skills</h1>
          <p className="panel-kicker">
            methodology the agent retrieves into context · {pinned} pinned · {learned} learned
          </p>
        </div>
        <button className="secondary-button" type="button" onClick={() => void load()} disabled={loading} title="Refresh">
          <RefreshCw size={15} className={loading ? "spin" : ""} /> Refresh
        </button>
      </div>

      <div className="stage-body">
        <input
          className="playbook-search"
          type="search"
          placeholder="Search skills (name, description, tools)…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />

        {!enabled ? (
          <p className="empty-note">The skills index is disabled (set <code>skills.enabled: true</code>).</p>
        ) : filtered.length === 0 ? (
          <p className="empty-note">
            {playbooks.length === 0
              ? "No playbooks yet — author a SKILL.md, or let the agent emit one from a run."
              : "No playbooks match your search."}
          </p>
        ) : (
          <ul className="playbook-list">
            {filtered.map((p) => (
              <li key={p.id} className="playbook-card">
                <div className="playbook-main">
                  <div className="playbook-title">
                    {p.source === "disk" ? (
                      <span className="playbook-badge pinned" title="Pinned SKILL.md (re-seeded on boot)">
                        <Pin size={12} /> pinned
                      </span>
                    ) : (
                      <span className="playbook-badge learned" title="Agent-emitted (curated/decaying)">
                        <Sparkles size={12} /> learned
                      </span>
                    )}
                    <strong>{p.name}</strong>
                  </div>
                  <p className="playbook-desc">{p.description}</p>
                  {p.tools_used.length ? (
                    <div className="playbook-tools">
                      {p.tools_used.map((t) => (
                        <code key={t}>{t}</code>
                      ))}
                    </div>
                  ) : null}
                </div>
                <div className="playbook-meta">
                  <span title="confidence">conf {Math.round((p.confidence ?? 1) * 100)}%</span>
                  <span title="last used">used {ago(p.last_used)}</span>
                  <button
                    type="button"
                    className="icon-button danger"
                    title={p.source === "disk" ? "Delete (re-seeds from SKILL.md on restart)" : "Delete skill"}
                    onClick={() => setPending(p)}
                    data-testid={`playbook-delete-${p.id}`}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <ConfirmDialog
        open={pending !== null}
        title="Delete skill?"
        message={
          pending
            ? `Remove "${pending.name}"${pending.source === "disk" ? " — note: a pinned SKILL.md re-seeds on the next restart." : "."}`
            : undefined
        }
        confirmLabel="Delete"
        onConfirm={() => void confirmDelete()}
        onCancel={() => setPending(null)}
      />

      <p className="playbook-foot">
        <BookMarked size={13} /> Skills (`SKILL.md`) are methodology the agent <strong>retrieves</strong> into
        context (ADR 0009) — they advise, they don't run. For deterministic step-by-step runs
        across subagents, see <strong>Studio → Workflows</strong>.
      </p>
    </section>
  );
}
