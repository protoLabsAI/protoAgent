import { QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./app/App";
import { AppToasts } from "./app/AppToasts";
import { PairingConfirmation } from "./app/PairingConfirmation";
import { PairingRecovery } from "./app/PairingRecovery";
import { Launcher } from "./app/Launcher";
import { AppCrash, ForcedQuotaCrash } from "./app/AppCrash";
import { ErrorBoundary } from "./app/ErrorBoundary";
import { isLauncherWindow } from "./lib/desktop";
// ADR 0037 — design-system foundation. Order matters: brand tokens (--pl-*) load first,
// then the DS component styles, then the legacy theme.css (which may reference --pl-*).
import "@protolabsai/design/css/tokens";
import "@protolabsai/ui/styles.css"; // component styles, incl. the DS `.pl-markdown` renderer
import "streamdown/styles.css"; // streaming per-token fade (opt-in; see DS <Markdown> docstring)
import "katex/dist/katex.min.css"; // KaTeX glyph layout for math in the DS <Markdown>
import "./app/theme-base.css"; // shared token bridge + resets — must load before the rest
import "./app/theme.css";
import "./app/mobile-native.css"; // touch-input layer (≤767px) — must load LAST to win over the DS
import "./app/tablet.css"; // tablet band of the desktop shell (#4336 S5) — portrait overlay + touch floor
import { activateSlugAgent } from "./lib/api";
import { queryClient } from "./lib/queryClient";
import { watchThemeChanges } from "./lib/agentTheme";
import { migrateLegacyDeviceToken, redeemPairingFromUrl } from "./lib/pairing";
import { startDeviceTokenRotation } from "./lib/deviceRotation";
import { installStorageEviction } from "./lib/storageEviction";
import { initTranscriptBackend, transcriptStoreIfOpen } from "./chat/chat-store";
import { initPaletteThreads } from "./app/paletteChatStore";
import { setSyncCursorStore } from "./lib/syncCursor";
import { markBoot } from "./lib/bootTiming";

// First executable line of the bundle (bd-aawcj): the clock for "how long did this switch take
// to paint and become interactive". App marks first-render + ready and logs the segments.
markBoot("script-start");

// Back the live-sync change-feed cursor (ADR 0130 D3) with the shared transcript IndexedDB
// connection, so an EventSource resume survives a closed tab. A live accessor: it returns
// null until the store opens (the sessionStorage mirror stands in meanwhile) and follows a
// tenant switch's reopen.
setSyncCursorStore(transcriptStoreIfOpen);

// A phone paired before #4336 S3 holds its device token in the OPERATOR key. Move it to its
// own key before anything sends a request. Copy-then-remove, so a failed write leaves the
// old key authenticating exactly as before — nobody is signed out by the upgrade.
migrateLegacyDeviceToken();

// A full localStorage quota frees old agents' saved chats before giving up on a write (ADR 0114 D5).
installStorageEviction();

watchThemeChanges(); // fire `protoagent:theme` on any theme change → plugin iframes repaint live

// The desktop quick-launcher window (ADR 0057) boots the same bundle but renders ONLY the
// command palette — no shell, no slug activation. Everything else is the full console.
// The `is-launcher` class scopes launcher.css (its `.pl-cmdk-*` overrides are global in the
// bundle, so without this they'd leak onto the in-app palette in the main window).
const launcher = isLauncherWindow();
if (launcher) document.documentElement.classList.add("is-launcher");
if (!launcher) void activateSlugAgent(); // cold-agent resume + keep-warm touch on slug navigation (#806)

// Device pairing (ADR 0087): a `#pair=<code>` fragment is redeemed for a device token
// BEFORE React mounts, so a freshly scanned phone never flashes the "enter your token" gate
// on its way in. The code is stripped from the URL + history either way — a spent one left
// in the address bar leaks into screenshots for no benefit.
//
// `.finally` rather than top-level await: TLA constrains the build target, and this must
// render even if the claim rejects. With no `#pair=` fragment the promise resolves
// synchronously-ish (no network call at all), so the normal boot is unaffected.
let pairingError: string | null = null;
// Chat transcripts load from IndexedDB before anything mounts (epic #4981 S2): no surface may
// read or change a chat that hasn't loaded. Bounded (TRANSCRIPT_BOOT_DEADLINE_MS); on any
// failure the console keeps localStorage. Never rejects.
const transcriptsReady = initTranscriptBackend()
  .then(async (backend) => {
    // Palette / DM threads too (S3), bounded on their own: a stall here must not hold the mount.
    if (backend === "idb") {
      let late = false;
      await Promise.race([
        initPaletteThreads(transcriptStoreIfOpen(), () => late),
        new Promise((r) => setTimeout(() => r((late = true)), 3_000)),
      ]);
    }
  })
  .catch(() => {});
void Promise.all([
  redeemPairingFromUrl().then((res) => {
    if (res && !res.ok) pairingError = res.error;
  }).catch(() => {}),
  transcriptsReady,
])
  .finally(() => {
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {/* The ROOT boundary (#872): anything a panel boundary doesn't catch lands on a
        full-page recovery card instead of a white screen. Outermost so a throw in
        the providers themselves is caught too. */}
    <ErrorBoundary fallback={({ error }) => <AppCrash error={error} />}>
      {/* Inert unless the e2e/QA hook is set (ADR 0114) — reaches AppCrash's quota path. */}
      <ForcedQuotaCrash />
      <QueryClientProvider client={queryClient}>
        <AppToasts>
          <PairingConfirmation />
          <PairingRecovery error={pairingError}>{launcher ? <Launcher /> : <App />}</PairingRecovery>
        </AppToasts>
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>,
);
// A paired phone rotates its token weekly (ADR 0121 D5): asked on boot — after any `#pair=`
// claim above has stored the token — and daily while open. Not in the launcher window.
if (!launcher) startDeviceTokenRotation();
});
