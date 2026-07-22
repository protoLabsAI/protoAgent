import { Dialog } from "@protolabsai/ui/overlays";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { previewMcpSummary, previewSecretsSummary } from "../lib/archetypeConfig";
import { errMsg } from "../lib/format";
import type { Archetype, ArchetypePreviewMember } from "../lib/types";

// "What's included" — the read-only pre-pick preview of an archetype: the full
// base SOUL plus, for bundle-backed archetypes, the bundle's members with each
// one's skills/pip-deps/capabilities (GET /api/archetypes/{id}/preview — a peek,
// nothing installs). Shared by the setup wizard and the fleet new-agent panel.

function MemberCard({ member }: { member: ArchetypePreviewMember }) {
  if (member.error) {
    return (
      <div className="archetype-preview-member">
        <div className="archetype-preview-member-head">
          <strong>{member.id ?? "unknown"}</strong>
          <span className="archetype-preview-muted">unreachable — {member.error}</span>
        </div>
      </div>
    );
  }
  const pip = member.requires_pip ?? [];
  const caps = member.capabilities ?? {};
  const capBits: string[] = [];
  const net = caps["network"];
  if (Array.isArray(net) && net.length) capBits.push(`network: ${net.join(", ")}`);
  if (caps["filesystem"] && caps["filesystem"] !== "none") capBits.push(`filesystem: ${String(caps["filesystem"])}`);
  return (
    <div className="archetype-preview-member">
      <div className="archetype-preview-member-head">
        <strong>{member.name ?? member.id}</strong>
        <span className="archetype-preview-muted">
          {member.version ? `v${member.version}` : null}
          {member.builtin ? " · built-in" : member.ref ? ` · ${member.ref}` : null}
        </span>
      </div>
      {member.description ? <p className="archetype-preview-desc">{member.description}</p> : null}
      {member.skills?.length ? (
        <div className="archetype-preview-skills">
          {member.skills.map((s) => (
            <span key={s.name} className="archetype-preview-chip" title={s.description}>
              /{s.name}
            </span>
          ))}
        </div>
      ) : null}
      {pip.length || capBits.length ? (
        <p className="archetype-preview-muted">
          {pip.length ? `pip: ${pip.join(", ")}` : null}
          {pip.length && capBits.length ? " · " : null}
          {capBits.join(" · ")}
        </p>
      ) : null}
    </div>
  );
}

export function ArchetypePreviewDialog({ archetype, onClose }: { archetype: Archetype; onClose: () => void }) {
  const preview = useQuery({
    queryKey: ["archetype-preview", archetype.id],
    queryFn: () => api.archetypePreview(archetype.id),
    enabled: Boolean(archetype.bundle),
    staleTime: 10 * 60 * 1000, // server peek is TTL-cached too
    retry: 1,
  });

  return (
    <Dialog open onClose={onClose} title={`What's included — ${archetype.label}`} width="min(680px, 95vw)">
      <div className="archetype-preview">
        <p className="archetype-preview-desc">{archetype.blurb}</p>

        {archetype.bundle ? (
          <section>
            <p className="fleet-section-label">Plugins &amp; skills</p>
            {preview.isLoading ? <p className="archetype-preview-muted">Reading the bundle…</p> : null}
            {preview.isError ? (
              <p className="archetype-preview-muted">
                Couldn&apos;t read the bundle right now ({errMsg(preview.error)}) — it installs from{" "}
                <code>{archetype.bundle}</code>.
              </p>
            ) : null}
            {preview.data?.bundle ? (
              <>
                {preview.data.bundle.description ? (
                  <p className="archetype-preview-desc">{preview.data.bundle.description}</p>
                ) : null}
                <div className="archetype-preview-members">
                  {preview.data.bundle.members.map((m, i) => (
                    <MemberCard key={m.id ?? i} member={m} />
                  ))}
                </div>
                {/* What the bundle asks the operator to supply (#2041) — pure display; the
                    new-agent Configure step collects these. */}
                {preview.data.bundle.mcp?.length ? (
                  <p className="archetype-preview-muted">
                    MCP servers: {previewMcpSummary(preview.data.bundle.mcp)}
                  </p>
                ) : null}
                {preview.data.bundle.secrets?.length ? (
                  <p className="archetype-preview-muted">
                    Secrets: {previewSecretsSummary(preview.data.bundle.secrets)}
                  </p>
                ) : null}
              </>
            ) : null}
          </section>
        ) : (
          <p className="archetype-preview-muted">Code-free persona — no plugins are installed for this archetype.</p>
        )}

        {archetype.soul ? (
          <section>
            <p className="fleet-section-label">Base SOUL.md</p>
            <pre className="archetype-preview-soul">{archetype.soul}</pre>
          </section>
        ) : null}
      </div>
    </Dialog>
  );
}
