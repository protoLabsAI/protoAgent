import "./identity.css";

import { Input, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { useToast } from "@protolabsai/ui/overlays";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useEffect, useState } from "react";
import { Eye, Pencil, Save } from "lucide-react";

import { PanelHeader } from "@protolabsai/ui/navigation";
import { Markdown } from "../chat/LazyMarkdown";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";

// Agent → Identity: who this agent is. The SOUL.md (persona) renders as Markdown by default and
// fills the panel; "Edit" flips it to a raw textarea. Saving routes the name through the canonical
// settings cascade (POST /api/settings — the same path every other field uses; `identity.name` is
// ui_hidden in the schema so this panel stays its single editor) and writes SOUL.md via POST
// /api/config, then hot-reloads. It does NOT touch `identity.operator` — that's owned solely by the
// Operator & access panel; echoing a cached copy here used to clobber fresh edits made there.

export function IdentityPanel() {
  const qc = useQueryClient();
  const toast = useToast();
  const { data, isLoading } = useQuery({ queryKey: ["config"], queryFn: () => api.config() });

  const [name, setName] = useState("");
  const [soul, setSoul] = useState("");
  const [seeded, setSeeded] = useState(false);
  const [editing, setEditing] = useState(false); // default: rendered Markdown

  useEffect(() => {
    if (data && !seeded) {
      setName(data.config?.identity?.name || "");
      setSoul(data.soul || "");
      setSeeded(true);
    }
  }, [data, seeded]);

  const baseName = data?.config?.identity?.name || "";
  const baseSoul = data?.soul || "";
  const dirty = seeded && (name !== baseName || soul !== baseSoul);

  const save = useMutation({
    mutationFn: async () => {
      // Name → the canonical schema cascade; SOUL (not a schema field) → /api/config with a null
      // config so nothing else in the config doc is touched. Only write what actually changed.
      if (name.trim() !== baseName) await api.saveSettings({ "identity.name": name.trim() });
      if (soul !== baseSoul) await api.applyConfig(null, soul);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: queryKeys.runtime });
      qc.invalidateQueries({ queryKey: queryKeys.settings });
      toast({ tone: "success", title: "Identity saved", message: "Agent reloaded." });
    },
    onError: () => toast({ tone: "error", title: "Save failed", message: "Check the server log." }),
  });

  return (
    <section className="panel stage-panel">
      <PanelHeader
        title="Identity"
        kicker="who this agent is — its name and persona (SOUL.md)"
        actions={
          <Button
            variant="primary"
            type="button"
            disabled={!dirty || save.isPending}
            onClick={() => save.mutate()}
            data-testid="identity-save"
          >
            <Save size={15} /> {save.isPending ? "Saving…" : "Save & reload"}
          </Button>
        }
      />
      <div className="stage-body identity-body">
        {isLoading || !seeded ? (
          <p className="muted">Loading…</p>
        ) : (
          <>
            <label className="field">
              <span>Agent name</span>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="my-agent" data-testid="identity-name" />
            </label>
            {/* What saving does — kept ABOVE the editor so the SOUL.md editor runs to the panel
                bottom. Save success/failure is reported via toast, not an inline strip. */}
            <p className="muted soul-hint">
              Saving updates the name (settings cascade) and writes SOUL.md, then hot-reloads the agent.
              The name updates the A2A card and console.
            </p>
            <div className="field soul-field">
              <div className="soul-head">
                <span>SOUL.md — the agent's persona &amp; system identity</span>
                <Button variant="ghost" type="button" onClick={() => setEditing((e) => !e)} data-testid="identity-soul-toggle">
                  {editing ? <><Eye size={14} /> Preview</> : <><Pencil size={14} /> Edit</>}
                </Button>
              </div>
              {editing ? (
                <Textarea
                  value={soul}
                  onChange={(e) => setSoul(e.target.value)}
                  spellCheck={false}
                  placeholder="# You are …"
                  data-testid="identity-soul"
                  className="soul-textarea"
                  style={{ fontFamily: "var(--font-mono, monospace)", fontSize: "13px", lineHeight: 1.5 }}
                />
              ) : (
                <div className="soul-preview markdown-body" data-testid="identity-soul-preview" onDoubleClick={() => setEditing(true)}>
                  <Markdown>{soul.trim() || "_Empty — click **Edit** to write this agent's persona._"}</Markdown>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
