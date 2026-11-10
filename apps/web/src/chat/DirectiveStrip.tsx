import { useEffect } from "react";
import { Button } from "@protolabsai/ui/primitives";
import { StatusDot } from "@protolabsai/ui/data";
import { useIsMobile } from "../lib/useIsMobile";
import { useUI } from "../state/uiStore";
import { useDirectives } from "./useDirectives";

function openWorkDirectives() {
  const ui = useUI.getState();
  ui.setSurfaceState("work", {
    ...ui.surfaceStates.work,
    section: "directives",
    itemId: undefined,
    scrollTop: 0,
    filters: { ...ui.surfaceStates.work?.filters, thisChat: true },
  });
  ui.setRightPanel("work");
  ui.setRightCollapsed(false);
}

/** Consume console /directives requests without mounting the legacy drawer. */
export function DirectiveWorkRequest({ sessionId }: { sessionId: string }) {
  const request = useUI((s) => s.directivesRequest);
  useEffect(() => {
    if (request?.sessionId !== sessionId) return;
    openWorkDirectives();
    useUI.setState({ directivesRequest: null });
  }, [request, sessionId]);
  return null;
}

export function DirectiveStrip({ sessionId }: { sessionId: string }) {
  const { state, active, refresh } = useDirectives(sessionId);
  const mobile = useIsMobile();
  const failed = state === "failed";
  const idle = !failed && (state === "loading" || active.length === 0);
  const count = `${active.length} active instruction${active.length === 1 ? "" : "s"}`;
  return <div className={`chat-ledger${idle ? " chat-ledger--idle" : ""}`} role="status"
    aria-label={idle || failed ? undefined : `${count}: ${active.map((d) => d.quote).join("; ")}`}>
    {!idle && <>
      <StatusDot />
      <span className="chat-ledger-list" title={active.map((d) => d.quote).join("\n")}>
        {failed ? "Couldn't check standing instructions" : <>
          <strong className="chat-ledger-count">{mobile ? `${active.length} active` : count}</strong>
          {` · "${active[0].quote}"${!mobile && active.length > 1 ? ` and ${active.length - 1} more` : ""}`}
        </>}
      </span>
      <Button variant="ghost" size="sm" className="chat-ledger-action" onClick={() => failed ? void refresh() : openWorkDirectives()}>
        {failed ? "Retry" : "View"}
      </Button>
    </>}
  </div>;
}
