import type { PythonRuntimePayload } from "../lib/types";

// Pure view model for <PythonRuntimeCard>, split out (like nodeRuntimeView) so the
// show/hide + progress logic is unit-testable without rendering. The card only earns
// space when the backend would actually use the runtime — a frozen desktop build
// (`python.needed`) — AND there's something to say or do: not provisioned, an install
// in flight, a stale document baseline, or an unsupported host. Source runs spawn
// their own interpreter, so the card is hidden there, always.
export type PythonRuntimeView =
  | { kind: "hidden" }
  | { kind: "unsupported" }
  | {
      kind: "action";
      installing: boolean;
      pct: number;
      message: string;
      error: string | null;
      /** Runtime present but its doc baseline predates the current pin list — offer a refresh. */
      stale: boolean;
    };

export function pythonRuntimeView(p: PythonRuntimePayload | undefined): PythonRuntimeView {
  if (!p || !p.python.needed) return { kind: "hidden" };
  const { python, install } = p;
  // An install in flight wins the display, even before status flips to "managed".
  if (install.state === "running") {
    return {
      kind: "action",
      installing: true,
      pct: install.pct,
      message: install.message || "installing…",
      error: null,
      stale: false,
    };
  }
  // Provisioned with a current baseline → nothing to prompt.
  if (python.managed && python.baseline_current) return { kind: "hidden" };
  if (!python.supported) return { kind: "unsupported" };
  return {
    kind: "action",
    installing: false,
    pct: 0,
    message: "",
    error: install.state === "error" ? install.error : null,
    stale: python.managed && !python.baseline_current,
  };
}
