import { Badge, Empty } from "@protolabsai/ui/primitives";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { useQuery } from "@tanstack/react-query";
import { Library } from "lucide-react";

import { RefreshButton } from "../app/ui-kit";
import { api } from "../lib/api";

// Global ▸ Shared Skills (ADR 0041 + 0048) — the box-shared skill library every agent
// on this machine reads (shared ∪ private, in layered mode). Read-only here: a skill
// enters the shared library by being PROMOTED from a workspace (Workspace ▸ Skills),
// which is the curated, explicit "shared brain, private hands" lift. The library
// LOCATION (`commons.path`) is a host-scoped field edited in Host ▸ Host config.

export function CommonsPanel() {
  const q = useQuery({ queryKey: ["playbooks"], queryFn: () => api.playbooks(), retry: false });
  const all = q.data?.playbooks ?? [];
  const commons = all.filter((p) => p.tier === "commons");
  const layered = all.some((p) => p.tier);

  return (
    <section className="panel stage-panel" data-testid="commons-panel">
      <PanelHeader
        title="Shared Skills"
        kicker={`the box-shared skill library · ${commons.length} skill${commons.length === 1 ? "" : "s"}`}
        actions={<RefreshButton onClick={() => void q.refetch()} busy={q.isFetching} />}
      />
      <div className="stage-body">
        {!layered ? (
          <Empty
            title="No shared skills in use"
            description="No agent on this box is in layered mode. Set an agent's Skill sharing to “layered” (Workspace ▸ Skills) to read + contribute to the shared skills library, and set its location in Host config (Shared skills location)."
          />
        ) : commons.length === 0 ? (
          <Empty
            title="No shared skills yet"
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
