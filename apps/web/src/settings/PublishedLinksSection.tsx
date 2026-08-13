import { Alert, Spinner } from "@protolabsai/ui/data";
import { useToast } from "@protolabsai/ui/overlays";
import { Button, Empty } from "@protolabsai/ui/primitives";
import { Link2Off, Share2 } from "lucide-react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys, publishedLinksQuery } from "../lib/queries";
import type { PublishedLink } from "../lib/types";

// Settings ▸ Publish's footer card (#2684) — rides the SettingsCategoryPanel `footer`
// seam, same placement as SecretsPanel's status card, so the endpoint-URL fields above
// and "what have I actually published with them" share one stage panel.
export function PublishedLinksSection() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery(publishedLinksQuery());

  const revoke = useMutation({
    mutationFn: (id: string) => api.revokePublishedLink(id),
    onSuccess: (res) => {
      if (res.ok) {
        toast({ title: "Link revoked", message: "It no longer serves." });
        void queryClient.invalidateQueries({ queryKey: queryKeys.publishedLinks });
        return;
      }
      // Failure path: nothing local changed (mark_revoked never ran), so no
      // invalidation — the list still correctly shows the link as live.
      const message =
        res.reason === "not_configured"
          ? "Revocation isn't configured on this instance yet — the link is still live."
          : res.error || res.reason || "revoke failed";
      toast({ tone: "error", title: "Couldn't revoke", message });
    },
    onError: (e) => toast({ tone: "error", title: "Couldn't revoke", message: errMsg(e) }),
  });

  if (isError) {
    return (
      <div className="publish-links" data-testid="published-links">
        <span className="settings-group-head">Published links</span>
        <Alert status="error">Couldn't load published links — check that the server is reachable.</Alert>
      </div>
    );
  }

  const links = data?.links ?? [];

  return (
    <div className="publish-links" data-testid="published-links">
      <span className="settings-group-head">Published links</span>
      <p className="setting-desc">
        Threads this instance has published to the hosted viewer. Revoking presents the
        stored token to the configured revoke endpoint — a link only stops serving once
        that's confirmed, not the moment you click.
      </p>

      {isLoading ? (
        <div className="devices-loading">
          <Spinner size={18} />
          <span>Loading published links…</span>
        </div>
      ) : links.length === 0 ? (
        <Empty
          icon={<Share2 size={20} />}
          title="Nothing published yet"
          description="Use Publish… from a chat tab to share a thread as a read-only link."
        />
      ) : (
        <div className="subagent-list">
          {links.map((link: PublishedLink) => (
            <div className="subagent-row" key={link.id}>
              <div>
                <strong>{link.title || link.thread_id}</strong>
                <span>
                  {link.revoked_at ? (
                    "revoked"
                  ) : (
                    <a href={link.public_url} target="_blank" rel="noreferrer">
                      {link.public_url}
                    </a>
                  )}
                </span>
              </div>
              <div className="issue-actions">
                {!link.revoked_at && (
                  <Button
                    icon
                    variant="ghost"
                    type="button"
                    title="Revoke"
                    aria-label={`Revoke ${link.title || link.thread_id}`}
                    loading={revoke.isPending && revoke.variables === link.id}
                    disabled={revoke.isPending}
                    onClick={() => revoke.mutate(link.id)}
                  >
                    <Link2Off size={15} />
                  </Button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
