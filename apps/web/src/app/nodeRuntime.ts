import type { NodeRuntimePayload } from "../lib/types";

// Pure view model for <NodeRuntimeCard>, split out so the show/hide + progress logic is
// unit-testable without rendering. The card is only worth showing when there's something
// to say or do: no Node to launch npx with, an install in flight, or an unsupported host.
// When a usable Node exists (system or managed) we render nothing — the panel stays clean.
export type NodeRuntimeView =
  | { kind: "hidden" }
  | { kind: "unsupported" }
  | { kind: "action"; installing: boolean; pct: number; message: string; error: string | null };

export function nodeRuntimeView(p: NodeRuntimePayload | undefined): NodeRuntimeView {
  if (!p) return { kind: "hidden" };
  const { node, install } = p;
  // An install in flight wins the display, even before status flips to "managed".
  if (install.state === "running") {
    return { kind: "action", installing: true, pct: install.pct, message: install.message || "installing…", error: null };
  }
  // A usable Node (the user's own, or one we provisioned) → nothing to prompt.
  if (node.source) return { kind: "hidden" };
  if (!node.supported) return { kind: "unsupported" };
  return {
    kind: "action",
    installing: false,
    pct: 0,
    message: "",
    error: install.state === "error" ? install.error : null,
  };
}
