import { Banner, Button } from "@protolabsai/ui/primitives";
import { useToast } from "@protolabsai/ui/overlays";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { Download } from "lucide-react";

import { pythonRuntimeQuery } from "../lib/queries";
import { pythonRuntimeView } from "./pythonRuntime";
import { api } from "../lib/api";
import { errMsg } from "../lib/format";

/** Managed Python runtime (ADR 0094). On the packaged desktop app the execute_code
 *  child needs a real interpreter (`sys.executable` is the frozen server binary), so
 *  every compute-through-code capability — cowork's document skills above all — is
 *  inert until one is provisioned (#2137). This surfaces one-click provisioning right
 *  where the gap bites (Settings ▸ Tools, beside the execute_code toggle). It renders
 *  nothing on source runs, and nothing once the runtime + document baseline are in
 *  place — only a call to action while there's a gap. */
export function PythonRuntimeCard() {
  const qc = useQueryClient();
  const toast = useToast();
  const { data } = useQuery(pythonRuntimeQuery());
  const view = pythonRuntimeView(data);

  const install = useMutation({
    mutationFn: () => api.installPythonRuntime(),
    onError: (err: unknown) => toast({ tone: "error", title: "Couldn't start install", message: errMsg(err) }),
    // The POST returns 202 immediately; refetch so the query flips to "running" and polls.
    onSettled: () => qc.invalidateQueries({ queryKey: pythonRuntimeQuery().queryKey }),
  });

  // When an install lands, tell the operator — execute_code calls that refused with the
  // install path can simply be retried now.
  const prev = useRef<string | undefined>(undefined);
  useEffect(() => {
    const state = data?.install.state;
    if (prev.current === "running" && state === "done") {
      toast({
        tone: "success",
        title: "Python runtime installed",
        message: "execute_code and the document skills (docx / xlsx / pptx / pdf) can now run.",
      });
    } else if (prev.current === "running" && state === "error") {
      toast({ tone: "error", title: "Install failed", message: data?.install.error ?? "unknown error" });
    }
    prev.current = state;
  }, [data?.install.state, data?.install.error, toast]);

  if (view.kind === "hidden") return null;

  if (view.kind === "unsupported") {
    return (
      <Banner tone="warning" title="No managed Python build for this platform" className="shell-warning-banner">
        <code>execute_code</code> — and the skills that author files through it — can't run on this
        desktop build.
      </Banner>
    );
  }

  const installing = view.installing || install.isPending;
  return (
    <Banner
      tone="warning"
      title={
        installing
          ? "Installing Python runtime…"
          : view.stale
            ? "Python runtime needs a refresh"
            : "No Python runtime provisioned"
      }
      className="shell-warning-banner"
      action={
        <Button size="sm" variant="primary" loading={installing} onClick={() => install.mutate()}>
          <Download size={14} /> {view.error ? "Retry install" : view.stale ? "Update runtime" : "Install runtime"}
        </Button>
      }
    >
      {installing ? (
        <>{view.message || "working…"} This runs once — CPython, then the document libraries.</>
      ) : view.error ? (
        <>
          Provisioning failed: {view.error}. <code>execute_code</code> (and the cowork document skills)
          can't run yet.
        </>
      ) : view.stale ? (
        <>
          The document baseline changed since this runtime was provisioned — update it so the
          document skills track the current library pins.
        </>
      ) : (
        <>
          On the desktop app, <code>execute_code</code> — and every skill that authors files through
          it (docx, xlsx, pptx, pdf) — needs a managed Python runtime. Provision it once (a
          hash-verified ~35 MB download plus the document libraries).
        </>
      )}
    </Banner>
  );
}
