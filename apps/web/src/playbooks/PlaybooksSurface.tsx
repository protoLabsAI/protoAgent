import { Input } from "@protolabsai/ui/forms";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { ArrowUpToLine, BookMarked, Library, Pin, Share2, Sparkles, Trash2 } from "lucide-react";

import { useEffect, useMemo, useState } from "react";

import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { RefreshButton } from "../app/ui-kit";
import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
import { QuickSetting } from "../settings/QuickSetting";
import type { Playbook } from "../lib/types";

// Playbooks surface (ADR 0009) — browse + manage the procedural-memory skill
// index (skills.db) the operator was otherwise blind to. "Playbooks" is the
// operator-facing name for skill-v1 artifacts: disk = pinned SKILL.md,
// emitted = agent-learned (curated/decaying). Functional-first: list + search +
// delete-with-confirm. Confidence tuning / curator audit are follow-ups.

export function PlaybooksSurface({ onError = () => {} }: { onError?: (message: string) => void }) {
  const [playbooks, setPlaybooks] = useState<Playbook[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [pending, setPending] = useState<Playbook | null>(null);
  const [promoting, setPromoting] = useState<number | null>(null);

  async function load() {
    setLoading(true);
    try {
      const r = await api.playbooks();
      setEnabled(r.enabled);
      setPlaybooks(r.playbooks || []);
      onError("");
    } catch (e) {
      onError(errMsg(e));
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
  // Tier is present only when the index is layered (commons ∪ private). When it
  // is, surface a commons count so the operator sees the shared library at a glance.
  const layered = playbooks.some((p) => p.tier);
  const fromCommons = filtered.filter((p) => p.tier === "commons").length;

  async function promote(p: Playbook) {
    setPromoting(p.id);
    try {
      const r = await api.promotePlaybook(p.id);
      if (!r.promoted) {
        onError(r.error || "promote failed");
        return;
      }
      onError("");
      await load(); // the skill now also reads from the commons tier
    } catch (e) {
      onError(errMsg(e));
    } finally {
      setPromoting(null);
    }
  }

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
      onError(errMsg(e));
    }
  }

  return (
    <section className="panel stage-panel" data-testid="playbooks-surface">
      <PanelHeader
        title="Skills"
        kicker={`methodology the agent retrieves into context · ${pinned} pinned · ${learned} learned${layered ? ` · ${fromCommons} from commons` : ""}`}
        actions={
          <>
            {/* Quick-set the skill-sharing mode (scoped/shared/layered) right where you
                manage skills — same field as Workspace ▸ Skills, ADR 0048. */}
            <QuickSetting keys={["skills.scope"]} title="Skill sharing" label="Skill sharing mode" icon={<Share2 size={16} />} />
            <RefreshButton onClick={() => void load()} busy={loading} />
          </>
        }
      />

      <div className="stage-body">
        <Input
          className="playbook-search"
          type="search"
          placeholder="Search skills (name, description, tools)…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />

        {!enabled ? (
          <Empty>The skills index is disabled (set <code>skills.enabled: true</code>).</Empty>
        ) : filtered.length === 0 ? (
          playbooks.length === 0 ? (
            <Empty
              title="No playbooks yet"
              description="author a SKILL.md, or let the agent emit one from a run."
            />
          ) : (
            <Empty>No playbooks match your search.</Empty>
          )
        ) : (
          <ul className="playbook-list">
            {filtered.map((p) => (
              <li key={p.id} className="playbook-card">
                <div className="playbook-main">
                  <div className="playbook-title">
                    {p.source === "disk" ? (
                      <span title="Pinned SKILL.md (re-seeded on boot)">
                        <Badge status="info">
                          <Pin size={12} /> pinned
                        </Badge>
                      </span>
                    ) : (
                      <span title="Agent-emitted (curated/decaying)">
                        <Badge status="success">
                          <Sparkles size={12} /> learned
                        </Badge>
                      </span>
                    )}
                    {p.tier === "commons" ? (
                      <span title="Shared commons — readable by every agent on this box">
                        <Badge status="neutral">
                          <Library size={12} /> commons
                        </Badge>
                      </span>
                    ) : p.tier === "private" ? (
                      <span title="Private to this agent — promote to share it with the fleet">
                        <Badge status="neutral">private</Badge>
                      </span>
                    ) : null}
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
                  {p.tier === "private" ? (
                    <Button
                      type="button"
                      icon variant="ghost"
                      title="Promote to the shared commons (every agent on this box can then reuse it)"
                      onClick={() => void promote(p)}
                      disabled={promoting === p.id}
                      data-testid={`playbook-promote-${p.id}`}
                    >
                      <ArrowUpToLine size={14} className={promoting === p.id ? "spin" : ""} />
                    </Button>
                  ) : null}
                  <Button
                    type="button"
                    icon variant="danger"
                    title={p.source === "disk" ? "Delete (re-seeds from SKILL.md on restart)" : "Delete skill"}
                    onClick={() => setPending(p)}
                    data-testid={`playbook-delete-${p.id}`}
                  >
                    <Trash2 size={14} />
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <ConfirmDialog
        open={pending !== null}
        title="Delete skill?"
        confirmLabel="Delete"
        destructive
        onConfirm={() => void confirmDelete()}
        onClose={() => setPending(null)}
      >
        {pending
          ? `Remove "${pending.name}"${pending.source === "disk" ? " — note: a pinned SKILL.md re-seeds on the next restart." : "."}`
          : undefined}
      </ConfirmDialog>

      <p className="playbook-foot">
        <BookMarked size={13} /> Skills (`SKILL.md`) are methodology the agent <strong>retrieves</strong> into
        context — they advise, they don't run. For deterministic step-by-step runs
        across subagents, see <strong>Studio → Workflows</strong>.
      </p>
    </section>
  );
}
