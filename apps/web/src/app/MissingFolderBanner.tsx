import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Alert } from "@protolabsai/ui/data";
import { Button } from "@protolabsai/ui/primitives";
import { useToast } from "@protolabsai/ui/overlays";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { queryKeys } from "../lib/queries";
import { readKey, writeKey } from "../lib/storage";
import type { MissingFolder } from "../lib/types";
import { PathPicker } from "../settings/PathPicker";

export function MissingFolderBanners({ folders, scope }: { folders: MissingFolder[]; scope: string }) {
  const storageKey = `protoagent.missingFolders.dismissed:${scope}`;
  const [dismissed, setDismissed] = useState<string[]>(() => {
    try {
      const saved: unknown = JSON.parse(readKey("session", storageKey) || "[]");
      return Array.isArray(saved) ? saved.filter((v): v is string => typeof v === "string") : [];
    } catch { return []; }
  });
  return <>{folders.map((folder) => {
    const signature = JSON.stringify([folder.name, folder.path]);
    if (dismissed.includes(signature)) return null;
    return <MissingFolderBanner key={signature} folder={folder} onDismiss={() => {
      const next = [...dismissed, signature];
      setDismissed(next);
      writeKey("session", storageKey, JSON.stringify(next));
    }} />;
  })}</>;
}

export function MissingFolderBanner({ folder, onDismiss }: { folder: MissingFolder; onDismiss: () => void }) {
  const client = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [path, setPath] = useState(folder.path);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const act = async (action: "remove" | "relocate") => {
    setBusy(true);
    setError("");
    try {
      const result = await api.resolveMissingFolder({ ...folder, action, ...(action === "relocate" ? { new_path: path } : {}) });
      if (result.board_projects.length) {
        toast({ tone: "info", title: "Work folder updated", message: `Board projects still referencing the old path: ${result.board_projects.join(", ")}.` });
      }
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.runtime }),
        client.invalidateQueries({ queryKey: ["fs-projects"] }),
        client.invalidateQueries({ queryKey: ["managed-projects"] }),
      ]);
      setEditing(false);
    } catch (e) { setError(errMsg(e)); }
    finally { setBusy(false); }
  };
  return <Alert status="warning" className="shell-warning-banner">
    <div>Work folder not found: {folder.name} ({folder.path})</div>
    <Button size="sm" variant="default" disabled={busy} onClick={() => void act("remove")}>Remove from work folders</Button>
    <Button size="sm" variant="default" disabled={busy} onClick={() => setEditing(true)}>Choose its new location</Button>
    <Button size="sm" variant="ghost" disabled={busy} onClick={onDismiss}>Dismiss</Button>
    {editing && <div>
      <PathPicker value={path} onChange={setPath} kind="dir" autoBrowse ariaLabel={`New location for ${folder.name}`} />
      <Button size="sm" disabled={busy || !path.trim()} onClick={() => void act("relocate")}>Save location</Button>
      <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(false)}>Cancel</Button>
    </div>}
    {error && <div role="alert">{error}</div>}
  </Alert>;
}
