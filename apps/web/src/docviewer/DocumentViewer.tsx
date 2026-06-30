import "./docviewer.css";

import { Dialog } from "@protolabsai/ui/overlays";
import { Spinner } from "@protolabsai/ui/data";
import { useEffect, useState } from "react";

import { Markdown } from "../chat/LazyMarkdown";
import { closeDocument, useDocViewer } from "./store";

// The single full-screen document reader (ADR 0062). Mounted once at the app root; shows
// whatever `openDocument(spec)` last set. Renders, in priority: a custom `render()` body,
// an async `load()`'d markdown doc, or inline `content`. Reuses the console Markdown
// renderer (same as chat + Activity) so reports/docs look identical wherever they're read.
export function DocumentViewer() {
  const { open, doc } = useDocViewer();
  const [body, setBody] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !doc || doc.render) return; // custom render owns its body
    if (!doc.load) {
      setBody(doc.content ?? "");
      setError(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    doc.load().then(
      (text) => {
        if (!cancelled) {
          setBody(text);
          setLoading(false);
        }
      },
      (e: unknown) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
          setLoading(false);
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [open, doc]);

  if (!open || !doc) return null;

  return (
    <Dialog
      open
      onClose={closeDocument}
      width="min(1100px, 96vw)"
      className="doc-viewer"
      title={
        <span className="doc-viewer__title">
          <span className="doc-viewer__heading">{doc.title}</span>
          {doc.subtitle ? <span className="doc-viewer__subtitle">{doc.subtitle}</span> : null}
        </span>
      }
    >
      <div className="doc-viewer__body">
        {doc.render ? (
          doc.render()
        ) : loading ? (
          <div className="doc-viewer__status">
            <Spinner size={16} /> Loading…
          </div>
        ) : error ? (
          <div className="doc-viewer__status" role="alert">
            Couldn’t load this document: {error}
          </div>
        ) : (
          <Markdown>{body}</Markdown>
        )}
      </div>
    </Dialog>
  );
}
