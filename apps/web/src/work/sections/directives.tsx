/**
 * The Directives work section (#4955 §2.11) — the standing-instructions ledger, rendered as a
 * Work section and registered into the Work view on import.
 *
 * This section keeps the approved ledger design: it reuses {@link DirectiveDrawerBody} verbatim,
 * so the fields, the "Never shown" list and every copy string follow directive-ledger-view.md
 * §2 and §4 (Q11). That means an in-force entry keeps the ledger's word **"Active"** under a
 * neutral group heading — the one §2.2 exception — and is NOT mapped to the running tone.
 * History keeps the ledger's own "done"/"withdrawn" badges; the done/cancelled mapping for
 * closed entries is used only for Overview counts, which the shared tone map already drives.
 *
 * The ledger is per-chat, so the body needs the focused session: the Work view passes it on
 * the section context. We fall back to the session carried by the first directive item (the
 * source is loaded for exactly one session). With no session in view there is nothing to show,
 * so the ledger's own empty copy stands in.
 */
import type { ReactNode } from "react";
import { Empty } from "@protolabsai/ui/primitives";

import { DirectiveDrawerBody } from "../../chat/DirectiveDrawer";
import { registerWorkSection, type WorkSectionContext } from "../WorkView";

function renderDirectives(ctx: WorkSectionContext): ReactNode {
  const sessionId = ctx.sessionId || ctx.sources.directives.items[0]?.sessionId;
  if (!sessionId) {
    return (
      <Empty
        title="No standing instructions"
        description="When you give this agent an instruction or approval that lasts beyond one reply, it records your words here. You can withdraw any of them at any time."
      />
    );
  }
  // Keyed by session so switching chats remounts the ledger against the new session's store.
  return (
    <div className="work-section work-section--directives">
      <DirectiveDrawerBody key={sessionId} sessionId={sessionId} />
    </div>
  );
}

registerWorkSection("directives", { label: "Directives", render: renderDirectives });
