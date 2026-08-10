import { useQuery } from "@tanstack/react-query";

import { OAUTH_PROVIDER_LABEL, OAuthAccountCard } from "../oauth/OAuthAccount";
import { runtimeStatusQuery } from "../lib/queries";

// Settings ▸ Model — the post-setup OAuth account lifecycle (#2460). Before
// this, Disconnect/Reconnect existed ONLY inside the Setup Wizard: once setup
// finished, there was no supported way to inspect or repair the connection
// (which turned the #2458/#2459 recovery paths into dead UI). Renders nothing
// for gateway/API-key providers.
export function OAuthAccountSection() {
  const { data } = useQuery(runtimeStatusQuery());
  const provider = (data?.model?.provider || "").trim().toLowerCase();
  const label = OAUTH_PROVIDER_LABEL[provider];
  if (!label) return null;

  return (
    <div className="settings-subsection" data-testid="oauth-account-section">
      <h2 className="panel-kicker">Connected account</h2>
      <p className="muted">
        This agent runs on your {label}. Disconnecting signs the agent out and disables chat
        until you reconnect — protoAgent removes its own stored credential; a login shared
        with your CLI is never revoked remotely.
      </p>
      <OAuthAccountCard provider={provider} />
    </div>
  );
}
