import { Button } from "@protolabsai/ui/primitives";
import { Input } from "@protolabsai/ui/forms";
import { Dialog } from "@protolabsai/ui/overlays";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";

import { api } from "../lib/api";
import { queryKeys, runtimeStatusQuery } from "../lib/queries";

const REGISTRY_GUIDE_URL = "https://protolabsai.github.io/protoAgent/guides/plugin-registry";

// Install a plugin from a git URL (ADR 0027) — the advanced path; the curated Discover
// directory is the primary way to add a plugin (ADR 0059 D4). Opened from the Installed
// tab toolbar. Installing AUTO-ENABLES + runs it (trust-by-default): its tools, console
// views and background surfaces come up live, no separate enable step and no restart.
export function InstallPluginDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [status, setStatus] = useState("");

  const install = useMutation({
    mutationFn: () => api.installPlugin(url.trim(), ref.trim() || undefined),
    onSuccess: (res) => {
      const s = res.installed;
      // Refresh the Installed list (runtime roster) AND the lock-backed inventory that
      // gates Uninstall.
      qc.invalidateQueries({ queryKey: runtimeStatusQuery().queryKey });
      qc.invalidateQueries({ queryKey: queryKeys.installedPlugins });
      // Install auto-enables the plugin, so its declared Settings fields (ADR 0019) are
      // now in the schema — refetch it or the plugin's config section won't appear until
      // a restart clears the 5-min-stale cache (#1423).
      qc.invalidateQueries({ queryKey: queryKeys.settings });
      setUrl("");
      setRef("");
      // Clean install (auto-enabled, nothing to flag) → close; the new row shows in the
      // list. If auto-enable failed or there are deps to install manually, keep the
      // dialog open with the note so it isn't lost.
      if (!res.enable_error && !s.requires_pip?.length) {
        onClose();
        return;
      }
      const who = res.enabled.length ? res.enabled.join(", ") : (s.id ?? "plugin");
      const deps = s.requires_pip?.length ? ` — declares deps (install manually): ${s.requires_pip.join(", ")}` : "";
      setStatus(
        res.enable_error
          ? `Installed ${who} — auto-enable failed (${res.enable_error}); enable it from the list${deps}`
          : `Installed ${who}${deps}`,
      );
    },
    onError: (e: unknown) => setStatus(e instanceof Error ? e.message : "install failed"),
  });

  if (!open) return null;
  return (
    <Dialog open onClose={onClose} title="Install a plugin from a git URL" width="min(620px, 94vw)">
      <p className="settings-section-sub">
        Installing <strong>enables and runs it</strong> immediately. Only install code you trust;
        for untrusted code use an{" "}
        <a href="https://protolabsai.github.io/protoAgent/guides/mcp" target="_blank" rel="noreferrer">MCP server</a>{" "}
        instead. <a href={REGISTRY_GUIDE_URL} target="_blank" rel="noreferrer">Guide</a>.
      </p>
      <div className="plugin-install-form">
        <Input
          type="text"
          placeholder="https://github.com/owner/protoagent-plugin-x"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          aria-label="plugin git URL"
        />
        <Input
          type="text"
          placeholder="ref (tag / sha — optional)"
          value={ref}
          onChange={(e) => setRef(e.target.value)}
          aria-label="git ref"
          style={{ maxWidth: 200 }}
        />
        <Button
          variant="primary"
          loading={install.isPending}
          disabled={!url.trim()}
          onClick={() => { setStatus(""); install.mutate(); }}
        >
          {install.isPending ? null : <Plus size={15} />} Install
        </Button>
      </div>
      {status ? <p className="plugin-install-status" role="status">{status}</p> : null}
    </Dialog>
  );
}
