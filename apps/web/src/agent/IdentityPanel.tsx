import { Button } from "@protolabsai/ui/primitives";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useEffect, useState } from "react";
import { Save } from "lucide-react";

import { PanelHeader } from "@protolabsai/ui/navigation";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";

// Agent → Identity: who this agent is. Edit the name + SOUL.md (persona) inline;
// saving merges the name into config, writes SOUL.md, and hot-reloads the graph
// (POST /api/config). Distinct from the read-only status snapshot in Settings → Overview.

export function IdentityPanel() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["config"], queryFn: () => api.config() });

  const [name, setName] = useState("");
  const [soul, setSoul] = useState("");
  const [seeded, setSeeded] = useState(false);

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
      <div className="stage-body">
        {isLoading || !seeded ? (
          <p className="muted">Loading…</p>
        ) : (
          <>
            <label className="field">
              <span>Agent name</span>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="my-agent" data-testid="identity-name" />
            </label>
            <label className="field">
              <span>SOUL.md — the agent's persona &amp; system identity</span>
              <textarea
                value={soul}
                onChange={(e) => setSoul(e.target.value)}
                rows={22}
                spellCheck={false}
                placeholder="# You are …"
                data-testid="identity-soul"
                style={{ fontFamily: "var(--font-mono, monospace)", fontSize: "13px", lineHeight: 1.5, width: "100%" }}
              />
            </label>
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
