import { Button } from "@protolabsai/ui/primitives";
import { Dialog, useToast } from "@protolabsai/ui/overlays";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ChevronDown, ChevronRight, History, RotateCcw } from "lucide-react";

import { api } from "../lib/api";
import type { SoulVersion } from "../lib/types";

// Version history for the agent's persona (#1691). Every SOUL.md save archives the outgoing
// text; this surfaces those snapshots as a restorable list ("toggle back"). It lives behind a
// trigger button + dialog so it stays out of the way until wanted — the button carries the
// snapshot count for discoverability. Restoring re-saves through the normal path, which
// snapshots the CURRENT persona first, so a roll-back is itself reversible; `onRestored` lets
// the parent editor re-seed from the new SOUL.

function whenLabel(saved_at: string): string {
  if (!saved_at) return "unknown time";
  const d = new Date(saved_at);
  return Number.isNaN(d.getTime()) ? "unknown time" : d.toLocaleString();
}

export function SoulHistoryButton({ onRestored }: { onRestored: () => void }) {
  const [open, setOpen] = useState(false);
  // Cheap read that also powers the count badge; the dialog's list shares this query key, so
  // opening it is instant and a restore's invalidation updates both the badge and the list.
  const { data } = useQuery({ queryKey: ["soul-history"], queryFn: () => api.soulHistory() });
  const count = data?.versions.length ?? 0;

  return (
    <>
      <Button
        variant="ghost"
        type="button"
        onClick={() => setOpen(true)}
        title="View and restore earlier versions of this persona"
        data-testid="soul-history-open"
      >
        <History size={14} /> History{count > 0 ? ` (${count})` : ""}
      </Button>
      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        title="Persona version history"
        width={620}
        className="soul-history-dialog"
      >
        <SoulHistoryList
          onRestored={() => {
            onRestored();
            setOpen(false); // the editor now shows the restored persona — that's the confirmation
          }}
        />
      </Dialog>
    </>
  );
}

function SoulHistoryList({ onRestored }: { onRestored: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["soul-history"],
    queryFn: () => api.soulHistory(),
  });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  const restore = useMutation({
    mutationFn: (id: string) => api.restoreSoulVersion(id),
    onSuccess: (res) => {
      setConfirming(null);
      qc.invalidateQueries({ queryKey: ["soul-history"] });
      const noop = res.messages?.some((m) => m.includes("already the current"));
      toast({
        tone: "success",
        title: noop ? "Already current" : "Version restored",
        message: noop ? "That version is already the live persona." : "Agent reloaded to this persona.",
      });
      if (!noop) onRestored();
    },
    onError: () => toast({ tone: "error", title: "Restore failed", message: "Check the server log." }),
  });

  if (isLoading) return <p className="muted soul-history-empty">Loading versions…</p>;
  if (isError) return <p className="muted soul-history-empty">Couldn't load version history.</p>;

  const versions: SoulVersion[] = data?.versions ?? [];
  if (versions.length === 0) {
    return (
      <p className="muted soul-history-empty">
        No earlier versions yet — each time you save the persona, the previous one is archived here.
      </p>
    );
  }

  return (
    <ul className="soul-history-list" data-testid="soul-history">
      {versions.map((v) => {
        const isOpen = expanded === v.id;
        return (
          <li key={v.id} className={`soul-history-item${v.is_current ? " is-current" : ""}`}>
            <div className="soul-history-row">
              <button
                type="button"
                className="soul-history-toggle"
                aria-expanded={isOpen}
                onClick={() => setExpanded(isOpen ? null : v.id)}
                data-testid="soul-history-toggle"
              >
                {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                <span className="soul-history-when">{whenLabel(v.saved_at)}</span>
                {v.is_current && <span className="soul-history-badge">current</span>}
                <span className="soul-history-preview">{v.preview || "(empty persona)"}</span>
              </button>
              {!v.is_current &&
                (confirming === v.id ? (
                  <span className="soul-history-confirm">
                    <Button
                      variant="primary"
                      type="button"
                      disabled={restore.isPending}
                      onClick={() => restore.mutate(v.id)}
                      data-testid="soul-history-confirm"
                    >
                      {restore.isPending ? "Restoring…" : "Confirm"}
                    </Button>
                    <Button variant="ghost" type="button" onClick={() => setConfirming(null)}>
                      Cancel
                    </Button>
                  </span>
                ) : (
                  <Button
                    variant="ghost"
                    type="button"
                    onClick={() => setConfirming(v.id)}
                    title="Roll the persona back to this version"
                    data-testid="soul-history-restore"
                  >
                    <RotateCcw size={14} /> Restore
                  </Button>
                ))}
            </div>
            {isOpen && <VersionBody id={v.id} />}
          </li>
        );
      })}
    </ul>
  );
}

// The full text of one version, fetched on demand when a row is expanded.
function VersionBody({ id }: { id: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["soul-version", id],
    queryFn: () => api.soulVersion(id),
  });
  if (isLoading) return <p className="muted soul-history-body">Loading…</p>;
  if (isError) return <p className="muted soul-history-body">Couldn't load this version.</p>;
  return (
    <pre className="soul-history-body" data-testid="soul-history-body">
      {data?.content || "(empty persona)"}
    </pre>
  );
}
