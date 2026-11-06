import { Input, Switch, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { useToast } from "@protolabsai/ui/overlays";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { type CSSProperties, useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Pencil, Plus, RotateCcw, Save, Trash2 } from "lucide-react";

import { STARTER_PROMPTS } from "../chat/starterPrompts";
import { api } from "../lib/api";
import { queryKeys, runtimeStatusQuery } from "../lib/queries";

// Settings ▸ Chat ▸ "Starter prompts" (#4570, bd-4wss). The one console editor for the
// empty-chat starter chips — the schema keeps `chat.show_starter_prompts` /
// `chat.starter_prompts` ui_hidden so they render NOWHERE in the generic Behavior panel
// (#1076 one-editor-per-key), and this owns both. Unlike the rest of ChatSettingsPanel
// (per-device UI prefs), these save to THIS AGENT's config via the settings cascade, the
// same path IdentityPanel uses.
//
// `chat.starter_prompts` absent/null ⇒ the built-in defaults; `[]` ⇒ none shown; a list ⇒
// that exact list (max 8). The default state shows the built-ins read-only with a
// "Customize" button that copies them into an editable list; "Reset to defaults" saves null
// so the key is removed and the defaults return.

export type StarterPrompt = { label: string; prompt: string; note?: string };

const MAX_PROMPTS = 8;

// Cleaned, saveable form of the editable list: trimmed, blank notes dropped. null is the
// sentinel for "use the built-in defaults" (the save removes the key).
export function cleanStarterPrompts(list: StarterPrompt[] | null): StarterPrompt[] | null {
  if (list === null) return null;
  return list.map((r) => {
    const out: StarterPrompt = { label: r.label.trim(), prompt: r.prompt.trim() };
    const note = r.note?.trim();
    if (note) out.note = note;
    return out;
  });
}

export function StarterPromptsEditor() {
  const qc = useQueryClient();
  const toast = useToast();
  const { data, isLoading } = useQuery({ queryKey: ["config"], queryFn: () => api.config() });
  const { data: runtime } = useQuery(runtimeStatusQuery());

  const [show, setShow] = useState(true);
  // null ⇒ the default state (built-ins, read-only); an array ⇒ an explicit edited list
  // (including [] for "none shown").
  const [custom, setCustom] = useState<StarterPrompt[] | null>(null);
  const [seeded, setSeeded] = useState(false);

  useEffect(() => {
    if (data && !seeded) {
      setShow(data.config?.chat?.show_starter_prompts ?? true);
      const list = data.config?.chat?.starter_prompts;
      setCustom(list == null ? null : list.map((p) => ({ ...p })));
      setSeeded(true);
    }
  }, [data, seeded]);

  const baseShow = data?.config?.chat?.show_starter_prompts ?? true;
  const baseList = data?.config?.chat?.starter_prompts ?? null;

  // The read-only built-ins shown in the default state: the live resolved list when the
  // switch is on (runtime recomputes it each poll — with no custom list that IS the four
  // defaults), else the hard-coded fallback so the operator still sees WHAT the built-ins
  // are while they're hidden.
  const builtins: StarterPrompt[] = (show ? runtime?.starter_prompts : undefined) ?? [...STARTER_PROMPTS];

  const cleanedCustom = cleanStarterPrompts(custom);
  const listDirty = JSON.stringify(cleanedCustom) !== JSON.stringify(cleanStarterPrompts(baseList));
  const showDirty = show !== baseShow;
  const dirty = seeded && (showDirty || listDirty);
  // A row missing a label or prompt can't be saved (the server would drop it) — block Save
  // and say why, rather than silently losing the row.
  const hasBlankRow = custom !== null && custom.some((r) => !r.label.trim() || !r.prompt.trim());

  const save = useMutation({
    mutationFn: async () => {
      // Only the keys that actually changed, like IdentityPanel. A reset sends null (the
      // server removes the key and the built-in defaults return); an emptied list sends [].
      const updates: Record<string, unknown> = {};
      if (showDirty) updates["chat.show_starter_prompts"] = show;
      if (listDirty) updates["chat.starter_prompts"] = cleanedCustom;
      return api.saveSettings(updates);
    },
    onSuccess: (r) => {
      // /api/settings reports a refused save as `{ok:false, messages}` with a 200. Every
      // settings panel checks `ok`; so does this one — a refusal must not claim success.
      if (!r.ok) {
        toast({ tone: "error", title: "Save failed", message: r.messages.join(" · ") || "Check the server log." });
        return;
      }
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: queryKeys.runtime });
      qc.invalidateQueries({ queryKey: queryKeys.settings });
      toast({
        tone: "success",
        title: "Starter prompts saved",
        message: r.messages.join(" · ") || "The empty chat updates on the next poll.",
      });
    },
    onError: () => toast({ tone: "error", title: "Save failed", message: "Check the server log." }),
  });

  const canSave = dirty && !hasBlankRow && !save.isPending;

  const updateRow = (i: number, patch: Partial<StarterPrompt>) =>
    setCustom((cur) => (cur ? cur.map((r, j) => (j === i ? { ...r, ...patch } : r)) : cur));
  const moveRow = (i: number, delta: number) =>
    setCustom((cur) => {
      if (!cur) return cur;
      const j = i + delta;
      if (j < 0 || j >= cur.length) return cur;
      const next = [...cur];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  const removeRow = (i: number) => setCustom((cur) => (cur ? cur.filter((_, j) => j !== i) : cur));
  const addRow = () => setCustom((cur) => [...(cur ?? []), { label: "", prompt: "" }]);
  const customize = () => setCustom(builtins.map((p) => ({ ...p })));
  const resetToDefaults = () => setCustom(null);

  const rowStyle: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    gap: "var(--pl-space-2)",
    padding: "var(--pl-space-3)",
    border: "1px solid var(--pl-color-border)",
    borderRadius: "var(--pl-radius-md)",
  };

  return (
    <div
      data-testid="starter-prompts-editor"
      data-key="chat.starter_prompts"
      style={{ borderTop: "1px solid var(--pl-color-border)", paddingTop: "26px", marginTop: "var(--pl-space-2)" }}
    >
      <div className="setting-meta">
        <span className="setting-label">Starter prompts</span>
        <p className="setting-desc">
          The “Try one of these” chips a fresh chat shows. Unlike the settings above, these are saved to{" "}
          <strong>this agent’s config</strong> — everyone who opens this agent sees them — not just this device.
        </p>
      </div>

      {isLoading || !seeded ? (
        <p className="muted" style={{ marginTop: "var(--pl-space-3)" }}>
          Loading…
        </p>
      ) : (
        <>
          <div className="setting-row" data-key="chat.show_starter_prompts" style={{ paddingLeft: 0, paddingRight: 0 }}>
            <div className="setting-meta">
              <span className="setting-label">Show starter prompts</span>
              <p className="setting-desc">The “Try one of these” suggestions in an empty chat. Off hides them for this agent.</p>
            </div>
            <Switch
              id="chat-show-starter-prompts"
              data-testid="starter-show"
              checked={show}
              onCheckedChange={setShow}
              label={show ? "on" : "off"}
            />
          </div>

          {custom === null ? (
            <div style={{ marginTop: "var(--pl-space-2)" }}>
              <p className="setting-desc" data-testid="starter-using-defaults">
                Using the built-in suggestions.
              </p>
              <ul
                data-testid="starter-builtins"
                style={{ listStyle: "none", margin: "var(--pl-space-3) 0", padding: 0, display: "flex", flexDirection: "column", gap: "var(--pl-space-2)" }}
              >
                {builtins.map((p, i) => (
                  <li key={`${p.label}-${i}`} style={rowStyle}>
                    <span className="setting-label">{p.label}</span>
                    <span className="setting-desc" style={{ margin: 0 }}>
                      {p.prompt}
                    </span>
                    {p.note ? (
                      <span className="setting-desc" style={{ margin: 0, fontStyle: "italic" }}>
                        {p.note}
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
              <Button type="button" onClick={customize} data-testid="starter-customize">
                <Pencil /> Customize
              </Button>
            </div>
          ) : (
            <div style={{ marginTop: "var(--pl-space-2)" }}>
              <ul
                data-testid="starter-rows"
                style={{ listStyle: "none", margin: "var(--pl-space-3) 0", padding: 0, display: "flex", flexDirection: "column", gap: "var(--pl-space-3)" }}
              >
                {custom.map((row, i) => (
                  <li key={i} data-testid={`starter-row-${i}`} style={rowStyle}>
                    <label className="field" style={{ display: "flex", flexDirection: "column", gap: "var(--pl-space-1)" }}>
                      <span className="setting-label">Label</span>
                      <Input
                        value={row.label}
                        data-testid={`starter-label-${i}`}
                        placeholder="Take a quick tour"
                        onChange={(e) => updateRow(i, { label: e.target.value })}
                      />
                    </label>
                    <label className="field" style={{ display: "flex", flexDirection: "column", gap: "var(--pl-space-1)" }}>
                      <span className="setting-label">Prompt</span>
                      <Textarea
                        value={row.prompt}
                        data-testid={`starter-prompt-${i}`}
                        placeholder="What can you do? Give me a quick tour of your tools."
                        rows={2}
                        onChange={(e) => updateRow(i, { prompt: e.target.value })}
                      />
                    </label>
                    <label className="field" style={{ display: "flex", flexDirection: "column", gap: "var(--pl-space-1)" }}>
                      <span className="setting-label">Note (optional)</span>
                      <Input
                        value={row.note ?? ""}
                        data-testid={`starter-note-${i}`}
                        placeholder="Needs Claude Code installed"
                        onChange={(e) => updateRow(i, { note: e.target.value })}
                      />
                    </label>
                    <div style={{ display: "flex", gap: "var(--pl-space-2)" }}>
                      <Button
                        variant="ghost"
                        type="button"
                        aria-label="Move up"
                        data-testid={`starter-up-${i}`}
                        disabled={i === 0}
                        onClick={() => moveRow(i, -1)}
                      >
                        <ArrowUp />
                      </Button>
                      <Button
                        variant="ghost"
                        type="button"
                        aria-label="Move down"
                        data-testid={`starter-down-${i}`}
                        disabled={i === custom.length - 1}
                        onClick={() => moveRow(i, 1)}
                      >
                        <ArrowDown />
                      </Button>
                      <Button
                        variant="danger"
                        type="button"
                        aria-label="Remove"
                        data-testid={`starter-remove-${i}`}
                        onClick={() => removeRow(i)}
                      >
                        <Trash2 />
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>

              {hasBlankRow ? (
                <p className="setting-override-note" data-testid="starter-blank-warning">
                  Every prompt needs a label and a prompt before you can save.
                </p>
              ) : null}

              <div style={{ display: "flex", gap: "var(--pl-space-2)", flexWrap: "wrap" }}>
                <Button
                  type="button"
                  onClick={addRow}
                  disabled={custom.length >= MAX_PROMPTS}
                  data-testid="starter-add"
                >
                  <Plus /> Add prompt
                </Button>
                <Button variant="ghost" type="button" onClick={resetToDefaults} data-testid="starter-reset">
                  <RotateCcw /> Reset to defaults
                </Button>
              </div>
            </div>
          )}

          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "var(--pl-space-4)" }}>
            <Button
              variant="primary"
              type="button"
              disabled={!canSave}
              onClick={() => save.mutate()}
              data-testid="starter-save"
            >
              <Save /> {save.isPending ? "Saving…" : "Save"}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
