import { FileCode2 } from "lucide-react";

import { codeRefFromProps, refLabel } from "./codeRef";
import { openCode } from "./open";

// The `code-ref` chat component (ADR 0112): what the agent's `show_code` renders in the
// transcript — a compact chip (path:lines + the agent's "why"), click → the code pane. The
// LIVE turn also auto-opens the pane (codeviewer/live.ts); this chip is the way back to it,
// and the only way in on a phone, where nothing opens by itself (ADR 0086).
export function CodeRefChip({ props }: { props: Record<string, unknown> }) {
  const ref = codeRefFromProps(props);
  if (!ref) return <div className="chat-comp chat-comp-unknown">[code-ref: missing project/path]</div>;
  const label = refLabel(ref);
  return (
    <button
      type="button"
      className="code-ref-chip"
      data-testid="code-ref-chip"
      title={`Open ${ref.project}/${label} in the code pane`}
      onClick={() => openCode({ ...ref, source: "component" })}
    >
      <FileCode2 size={14} aria-hidden className="code-ref-chip__icon" />
      <span className="code-ref-chip__body">
        <span className="code-ref-chip__path">
          <span className="code-ref-chip__project">{ref.project}</span>
          <span className="code-ref-chip__sep">/</span>
          {label}
        </span>
        {ref.note ? <span className="code-ref-chip__note">{ref.note}</span> : null}
      </span>
    </button>
  );
}
