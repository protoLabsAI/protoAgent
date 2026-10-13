import { useToast } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";
import { useEffect } from "react";

import { tauriCore, tauriEvent } from "../lib/api/desktop";
import "./download-toast.css";

// "Your download landed" for the desktop app. A page can only START a download (a plugin's
// `<a download>`, a chat export) and never learns whether the file reached disk, so the
// artifact panel's "Download started" was the last word — and a slow save looked like nothing
// happened. The Tauri shell's download hook knows when the file is written and where, and
// emits `download:finished` (apps/desktop/src-tauri/src/downloads.rs). This turns that into a
// toast with Open (the OS default app for the file type) and Show in Finder. Browsers keep
// their own download UI; outside the shell this renders and listens for nothing.

export type DownloadFinished = { success: boolean; path?: string | null; name?: string | null };

const isMac = typeof navigator !== "undefined" && /Mac/i.test(navigator.platform || navigator.userAgent);

export function DownloadWatch() {
  const toast = useToast();

  useEffect(() => {
    const events = tauriEvent();
    if (!events) return;
    let unlisten: (() => void) | null = null;
    let disposed = false;
    void events
      .listen<DownloadFinished>("download:finished", ({ payload }) => {
        const name = payload.name || "File";
        if (!payload.success) {
          toast({ tone: "error", title: "Download failed", message: `${name} didn't save.` });
          return;
        }
        const path = payload.path;
        const run = (cmd: string) => {
          if (path) void tauriCore()?.invoke(cmd, { path }).catch(() => {});
        };
        toast({
          tone: "success",
          title: "Downloaded",
          duration: 10000,
          message: (
            <span className="download-toast">
              <span className="download-toast-name">{name}</span>
              {path ? (
                <span className="download-toast-actions">
                  <Button size="sm" variant="primary" onClick={() => run("open_download")}>
                    Open
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => run("reveal_download")}>
                    {isMac ? "Show in Finder" : "Show in folder"}
                  </Button>
                </span>
              ) : null}
            </span>
          ),
        });
      })
      .then((off) => {
        if (disposed) off();
        else unlisten = off;
      })
      .catch(() => {});
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [toast]);

  return null;
}
