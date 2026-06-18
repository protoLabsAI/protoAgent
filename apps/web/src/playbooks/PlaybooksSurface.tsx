import { Input, Textarea } from "@protolabsai/ui/forms";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { ArrowUpToLine, BookMarked, Library, Pencil, Pin, Plus, Share2, Sparkles, Trash2 } from "lucide-react";

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
// operator-facing name for skill-v1 artifacts: user = operator-authored SKILL.md,
// bundled = shipped example, learned = agent-emitted (curated/decaying).
// Full CRUD: author a new skill, edit one (editing a learned skill materializes
// it as a durable user SKILL.md), delete, promote to the shared commons. Bundled
// examples + commons skills are read-only.

type Draft = {
  name: string;
  description: string;
  body: string;
  tools: string;
  userFacing: boolean;
  slash: string;
};
const EMPTY_DRAFT: Draft = { name: "", description: "", body: "", tools: "", userFacing: false, slash: "" };

function SkillForm({
  draft,
  setDraft,
  onSave,
  onCancel,
  saving,
  saveLabel,
}: {
  draft: Draft;
  setDraft: (d: Draft) => void;
  onSave: () => void;
  onCancel: () => void;
  saving: boolean;
  saveLabel: string;
}) {
  const incomplete = !draft.name.trim() || !draft.description.trim() || !draft.body.trim();
  return (
    <div className="knowledge-chunk-form">
      <Input
        type="text"
        placeholder="name — e.g. “Release notes”"
        value={draft.name}
        onChange={(e) => setDraft({ ...draft, name: e.target.value })}
        aria-label="skill name"
      />
      <Input
        type="text"
        placeholder="description — when should the agent reach for this? (the trigger signal)"
        value={draft.description}
        onChange={(e) => setDraft({ ...draft, description: e.target.value })}
        aria-label="skill description"
      />
      <Textarea
        rows={8}
        placeholder="The procedure — markdown instructions the agent retrieves into context."
        value={draft.body}
        onChange={(e) => setDraft({ ...draft, body: e.target.value })}
        aria-label="skill body"
      />
      <Input
        type="text"
        placeholder="tools (comma-separated, optional)"
        value={draft.tools}
        onChange={(e) => setDraft({ ...draft, tools: e.target.value })}
        aria-label="skill tools"
      />
      <div className="knowledge-chunk-form-row">
        <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={draft.userFacing}
            onChange={(e) => setDraft({ ...draft, userFacing: e.target.checked })}
            aria-label="invokable as a slash command"
          />
          Invokable as a <code>/slash</code> command
        </label>
        {draft.userFacing ? (
          <Input
            type="text"
            placeholder="slash token (optional — defaults to the name)"
            value={draft.slash}
            onChange={(e) => setDraft({ ...draft, slash: e.target.value })}
            aria-label="slash token"
            style={{ maxWidth: 260 }}
          />
        ) : null}
      </div>
      <div className="knowledge-chunk-form-row">
        <Button type="button" variant="primary" size="sm" disabled={saving || incomplete} onClick={onSave}>
          {saveLabel}
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

// An operator can edit user-authored + agent-learned skills; bundled examples and
// shared commons skills are read-only. Tolerate older payloads (no `origin`) by
// falling back to the source: a flat "disk" skill we can't classify is treated as
// editable so the affordance never silently disappears for non-layered stores.
function isEditable(p: Playbook): boolean {
  if (typeof p.editable === "boolean") return p.editable;
  if (p.tier === "commons") return false;
  return p.source !== "disk";
}

// Source badge — what KIND of skill this is, independent of its tier: "yours"
// (operator-authored SKILL.md), "pinned" (bundled example, read-only), or
// "learned" (agent-emitted). The shared/private TIER is a separate badge.
function SourceBadge({ p }: { p: Playbook }) {
  if (p.source === "disk") {
    return p.origin === "user" ? (
      <span title="You authored this skill (SKILL.md under your data home)">
        <Badge status="success">
          <Pencil size={12} /> yours
        </Badge>
      </span>
    ) : (
      <span title="Bundled example (re-seeded from SKILL.md on boot) — read-only">
        <Badge status="info">
          <Pin size={12} /> pinned
        </Badge>
      </span>
    );
  }
  return (
    <span title="Agent-emitted (curated/decaying)">
      <Badge status="neutral">
        <Sparkles size={12} /> learned
      </Badge>
    </span>
  );
}

export function PlaybooksSurface({ onError = () => {} }: { onError?: (message: string) => void }) {
  const [playbooks, setPlaybooks] = useState<Playbook[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [pending, setPending] = useState<Playbook | null>(null);
  const [promoting, setPromoting] = useState<number | null>(null);

  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);

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

  function openCreate() {
    setEditingId(null);
    setDraft(EMPTY_DRAFT);
    setAdding((v) => !v);
  }

  async function startEdit(p: Playbook) {
    setAdding(false);
    try {
      const r = await api.getPlaybook(p.id);
      const s = r.skill;
      if (!s) {
        onError("could not load that skill");
        return;
      }
      setDraft({
        name: s.name,
        description: s.description,
        body: s.prompt_template || "",
        tools: (s.tools_used || []).join(", "),
        userFacing: !!s.user_facing,
        slash: s.slash || "",
      });
      setEditingId(p.id);
      onError("");
    } catch (e) {
      onError(errMsg(e));
    }
  }

  function cancelForm() {
    setAdding(false);
    setEditingId(null);
    setDraft(EMPTY_DRAFT);
  }

  async function save() {
    setSaving(true);
    try {
      const payload = {
        name: draft.name.trim(),
        description: draft.description.trim(),
        prompt_template: draft.body,
        tools_used: draft.tools
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        user_facing: draft.userFacing,
        slash: draft.slash.trim(),
      };
      const r = editingId !== null ? await api.updatePlaybook(editingId, payload) : await api.createPlaybook(payload);
      if (!r.skill) {
        onError("save failed");
        return;
      }
      cancelForm();
      onError("");
      await load();
    } catch (e) {
      onError(errMsg(e));
    } finally {
      setSaving(false);
    }
  }

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
            {enabled ? (
              <Button
                icon
                variant="ghost"
                type="button"
                onClick={openCreate}
                title="Author a new skill"
                data-testid="playbook-new"
              >
                <Plus size={16} />
              </Button>
            ) : null}
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

        {adding ? (
          <SkillForm
            draft={draft}
            setDraft={setDraft}
            onSave={() => void save()}
            onCancel={cancelForm}
            saving={saving}
            saveLabel="Create skill"
          />
        ) : null}

        {!enabled ? (
          <Empty>The skills index is disabled (set <code>skills.enabled: true</code>).</Empty>
        ) : filtered.length === 0 ? (
          playbooks.length === 0 ? (
            <Empty
              title="No skills yet"
              description="author one with +, drop a SKILL.md, or let the agent emit one from a run."
            />
          ) : (
            <Empty>No skills match your search.</Empty>
          )
        ) : (
          <ul className="playbook-list">
            {filtered.map((p) => (
              <li key={p.id} className="playbook-card">
                {editingId === p.id ? (
                  <SkillForm
                    draft={draft}
                    setDraft={setDraft}
                    onSave={() => void save()}
                    onCancel={cancelForm}
                    saving={saving}
                    saveLabel="Save changes"
                  />
                ) : (
                  <>
                    <div className="playbook-main">
                      <div className="playbook-title">
                        <SourceBadge p={p} />
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
                        {p.user_facing ? (
                          <span title={`Invokable as /${p.slash || p.name}`}>
                            <Badge status="neutral">/{p.slash || p.name}</Badge>
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
                          icon
                          variant="ghost"
                          title="Promote to the shared commons (every agent on this box can then reuse it)"
                          onClick={() => void promote(p)}
                          disabled={promoting === p.id}
                          data-testid={`playbook-promote-${p.id}`}
                        >
                          <ArrowUpToLine size={14} className={promoting === p.id ? "spin" : ""} />
                        </Button>
                      ) : null}
                      {isEditable(p) ? (
                        <>
                          <Button
                            type="button"
                            icon
                            variant="ghost"
                            title="Edit skill"
                            onClick={() => void startEdit(p)}
                            data-testid={`playbook-edit-${p.id}`}
                          >
                            <Pencil size={14} />
                          </Button>
                          <Button
                            type="button"
                            icon
                            variant="danger"
                            title="Delete skill"
                            onClick={() => setPending(p)}
                            data-testid={`playbook-delete-${p.id}`}
                          >
                            <Trash2 size={14} />
                          </Button>
                        </>
                      ) : (
                        <span className="playbook-readonly" title="Bundled/shared skill — read-only here">
                          read-only
                        </span>
                      )}
                    </div>
                  </>
                )}
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
          ? `Remove "${pending.name}"${pending.origin === "user" ? " — its SKILL.md is deleted too." : "."}`
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
