import "./identity.css";

import { Input, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useEffect, useState } from "react";
import { Eye, Pencil, Save } from "lucide-react";

import { PanelHeader } from "@protolabsai/ui/navigation";
import { Markdown } from "../chat/LazyMarkdown";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";

// Agent → Identity: who this agent is. The SOUL.md (persona) renders as Markdown by default and
// fills the panel; "Edit" flips it to a raw textarea. Saving merges the name into config, writes
// SOUL.md, and hot-reloads the graph (POST /api/config).

export function IdentityPanel() {
  const qc = useQueryClient();
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
    mutationFn: () =>
      api.applyConfig(
        { identity: { name: name.trim(), operator: data?.config?.identity?.operator ?? "" } },
        soul,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: queryKeys.runtime });
    },
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
            {save.isError ? <p className="error-strip" role="alert">Save failed — check the server log.</p> : null}
            {save.isSuccess && !dirty ? <p className="muted">Saved — agent reloaded.</p> : null}
            <p className="muted" style={{ fontSize: "12px" }}>
              Saving writes SOUL.md + config and hot-reloads the agent. The name updates the A2A card and console.
            </p>
          </>
        )}
      </div>
    </section>
  );
}
