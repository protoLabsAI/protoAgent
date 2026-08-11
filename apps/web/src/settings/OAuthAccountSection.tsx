import { useQuery } from "@tanstack/react-query";

import { modelPickerData } from "../chat/modelForm";
import { OAUTH_PROVIDER_LABEL, OAuthAccountCard } from "../oauth/OAuthAccount";
import { runtimeStatusQuery, settingsSchemaQuery } from "../lib/queries";

// Settings ▸ Model — the post-setup OAuth account lifecycle (#2460). Before
// this, Disconnect/Reconnect existed ONLY inside the Setup Wizard: once setup
// finished, there was no supported way to inspect or repair the connection
// (which turned the #2458/#2459 recovery paths into dead UI). Renders nothing
// for gateway/API-key providers.
//
// Keyed on the SAVED provider (settings schema), not just the live one: a
// provider SWITCH persists the YAML first and reloads second, so a switch to a
// native provider with no credential fails the reload and leaves saved ≠ live —
// exactly when the sign-in card is needed most (Josh's live Claude/Codex
// switches, 2026-08-10). Signing in then completes the switch server-side
// (the oauth routes reload when the saved provider matches the fresh login).
export function OAuthAccountSection() {
  const { data: runtime } = useQuery(runtimeStatusQuery());
  const { data: schema } = useQuery(settingsSchemaQuery());
  const live = (runtime?.model?.provider || "").trim().toLowerCase();
  const saved = schema ? modelPickerData(schema.groups).provider : "";
  // Prefer the saved provider — after a failed switch it's the one needing auth.
  const provider = OAUTH_PROVIDER_LABEL[saved] ? saved : OAUTH_PROVIDER_LABEL[live] ? live : "";
  if (!provider) return null;
  const pendingSwitch = provider === saved && saved !== live && Boolean(live);

  return (
    <div className="settings-subsection settings-subsection--lead" data-testid="oauth-account-section">
      <h2 className="panel-kicker">Connected account</h2>
      <p className="muted">
        {pendingSwitch
          ? `Switching to your ${OAUTH_PROVIDER_LABEL[provider]} — the saved provider isn't live yet. Already signed in? Re-check completes the switch; otherwise sign in and it completes automatically.`
          : // Terse on purpose: the disconnect caveat lives on the Disconnect
            // button's tooltip — repeating it here doubled the section's height.
            `This agent runs on your ${OAUTH_PROVIDER_LABEL[provider]}.`}
      </p>
      <OAuthAccountCard provider={provider} />
    </div>
  );
}
