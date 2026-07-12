import { Alert } from "@protolabsai/ui/data";
import { useToast } from "@protolabsai/ui/overlays";
import { Badge, Button, Empty } from "@protolabsai/ui/primitives";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";

import { TestConnectionButton } from "../app/ui-kit";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys, secretsStatusQuery } from "../lib/queries";
import { SettingsCategoryPanel } from "./SettingsCategory";

// Settings ▸ Secrets (ADR 0080): the schema-driven `secrets_manager.*` fields plus a
// live status/actions card over /api/secrets/{status,sync,test}. The card rides the
// SettingsCategoryPanel `footer` seam so fields + status share ONE stage panel (two
// stacked panels split 50/50 and read as two surfaces — ADR 0048 §3.1).
export function SecretsPanel() {
  return <SettingsCategoryPanel category="Secrets" title="Secrets manager" footer={<SecretsStatusCard />} />;
}

function SecretsStatusCard() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const { data: status, isError } = useQuery(secretsStatusQuery());

  const sync = useMutation({
    mutationFn: () => api.secretsSync(),
    onSuccess: (s) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.secretsStatus });
      if (s.ok) toast({ tone: "success", title: "Secrets synced", message: `${s.applied} env var(s) live from ${s.provider}.` });
      else toast({ tone: "error", title: "Sync failed", message: s.error || s.error_kind || "fetch failed" });
    },
    onError: (e) => toast({ tone: "error", title: "Sync failed", message: errMsg(e) }),
  });

  const test = useMutation({
    mutationFn: () => api.secretsTest(),
    onSuccess: (r) => {
      if (r.ok) toast({ tone: "success", title: "Connection OK", message: `${r.count} secret(s) in scope.` });
      else toast({ tone: "error", title: "Connection failed", message: r.error || r.error_kind || "fetch failed" });
    },
    onError: (e) => toast({ tone: "error", title: "Connection failed", message: errMsg(e) }),
  });

  // A failed status query must not silently drop the card — say so instead.
  if (isError) {
    return (
      <div className="secrets-status" data-testid="secrets-status">
        <Alert status="error">Couldn’t load the secrets-manager status — check that the server is reachable.</Alert>
      </div>
    );
  }
  if (!status) return null;

  return (
    <div className="secrets-status" data-testid="secrets-status">
      <div className="secrets-status-head">
        <span className="settings-group-head">
          Manager status
          {status.enabled ? (
            status.ok ? (
              <Badge status="success">connected</Badge>
            ) : (
              <Badge status="warning">{status.error_kind || "error"}</Badge>
            )
          ) : (
            <Badge status="neutral">disabled</Badge>
          )}
        </span>
        <div className="secrets-status-actions">
          <TestConnectionButton onClick={() => test.mutate()} pending={test.isPending} />
          <Button
            type="button"
            variant="ghost"
            onClick={() => sync.mutate()}
            loading={sync.isPending}
            disabled={!status.enabled}
            title="Fetch from the manager and re-apply now"
          >
            {sync.isPending ? null : <RefreshCw size={15} />}
            Sync now
          </Button>
        </div>
      </div>
      {!status.enabled ? (
        <Empty>
          Enable the manager above and save — then Test connection, and Sync now to pull secrets into the environment.
        </Empty>
      ) : (
        <>
          {!status.ok && status.error ? <Alert status="error">{status.error}</Alert> : null}
          {status.shadowed.length > 0 ? (
            <Alert status="warning">
              Shadowed by pre-existing env vars ({status.shadowed.join(", ")}) — turn on “Manager beats existing env”
              to prefer the manager.
            </Alert>
          ) : null}
          <div className="secrets-status-meta">
            {status.applied} env var(s) manager-owned
            {status.fetched_at ? ` · last fetch ${status.fetched_at}` : ""}
            {status.refresh_seconds > 0 ? ` · refreshes every ${status.refresh_seconds}s` : " · boot/reload only"}
          </div>
          {status.vars.length > 0 ? (
            <div className="secrets-status-vars">
              {status.vars.map((v) => (
                <code key={v}>{v}</code>
              ))}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
