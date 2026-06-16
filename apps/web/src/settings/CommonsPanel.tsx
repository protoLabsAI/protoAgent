import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { useQuery } from "@tanstack/react-query";
import { Library, RefreshCw } from "lucide-react";

import { api } from "../lib/api";

// Global ▸ Commons (ADR 0041 + 0048) — the box-shared skill library every agent
// on this machine reads (commons ∪ private, in layered mode). Read-only here: a
// skill enters the commons by being PROMOTED from a workspace (Workspace ▸ Skills),
// which is the curated, explicit "shared brain, private hands" lift. The commons
// LOCATION (`commons.path`) is a host-scoped field edited in Host ▸ Host config.

export function CommonsPanel() {
  const q = useQuery({ queryKey: ["playbooks"], queryFn: () => api.playbooks(), retry: false });
  const all = q.data?.playbooks ?? [];
  const commons = all.filter((p) => p.tier === "commons");
  const layered = all.some((p) => p.tier);

  return (
    <section className="panel stage-panel" data-testid="commons-panel">
      <PanelHeader
        title="Commons"
        kicker={`the box-shared skill library · ${commons.length} skill${commons.length === 1 ? "" : "s"}`}
        actions={
          <Button icon variant="ghost" type="button" onClick={() => void q.refetch()} disabled={q.isFetching} title="Refresh">
            <RefreshCw size={16} className={q.isFetching ? "spin" : ""} />
          </Button>
        }
      />
      <div className="stage-body">
        {!layered ? (
          <Empty
            title="No commons in use"
            description="No agent on this box is in layered mode. Set an agent's Skill sharing to “layered” (Workspace ▸ Skills) to read + contribute to a shared commons, and choose its location in Host config (commons.path)."
          />
        ) : commons.length === 0 ? (
          <Empty
            title="The commons is empty"
            description="Promote a proven skill from a workspace (Workspace ▸ Skills ▸ Promote) to share it with every agent on this box."
          />
        ) : (
          <ul className="playbook-list">
            {commons.map((p) => (
              <li key={p.id} className="playbook-card">
                <div className="playbook-main">
                  <div className="playbook-title">
                    <span title="Shared commons — readable by every agent on this box">
                      <Badge status="neutral">
                        <Library size={12} /> commons
                      </Badge>
                    </span>
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
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
