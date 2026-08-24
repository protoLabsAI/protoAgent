import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import { modelPickerData } from "../chat/modelForm";
import { OAUTH_PROVIDER_LABEL, OAuthAccountCard } from "../oauth/OAuthAccount";
import { runtimeStatusQuery, settingsSchemaQuery } from "../lib/queries";

// Settings ▸ Model — the post-setup OAuth account lifecycle (#2460), now a LIST of
// every connected provider rather than a gate on the single active one (#3097).
// Before this, the section computed ONE provider from model.provider and returned
// null for everything else, so a second subscription you'd already connected stayed
// invisible unless you first switched model.provider to it. Now each signed-in
// native-OAuth provider (ADR 0097) gets its own connect/disconnect/reconnect card,
// listed alongside the gateway/API-key connection — independent of which one is the
// active default. This slice is presentation only: it doesn't change what
// model.provider/model.name mean, it just stops hiding providers you've connected.
//
// The active default still keys on the SAVED provider (settings schema), not just the
// live one: a provider SWITCH persists the YAML first and reloads second, so a switch
// to a native provider with no credential fails the reload and leaves saved ≠ live —
// exactly when the sign-in card is needed most (Josh's live Claude/Codex switches,
// 2026-08-10). Signing in then completes the switch server-side (the oauth routes
// reload when the saved provider matches the fresh login).
export function OAuthAccountSection() {
  const { data: runtime } = useQuery(runtimeStatusQuery());
  const { data: schema } = useQuery(settingsSchemaQuery());
  // The per-provider sign-in roster (ADR 0097) — always one entry per native OAuth
  // provider, each carrying its own signed_in flag; it's what lets the list show a
  // connected provider that ISN'T the active default.
  const { data: oauth } = useQuery({ queryKey: ["oauth-status"], queryFn: () => api.oauthStatus() });

  const live = (runtime?.model?.provider || "").trim().toLowerCase();
  const saved = schema ? modelPickerData(schema.groups).provider : "";
  // The active default provider — prefer the saved one (after a failed switch it's the
  // one still needing auth), then the live one, else whatever is configured at all.
  const active = OAUTH_PROVIDER_LABEL[saved] ? saved : OAUTH_PROVIDER_LABEL[live] ? live : saved || live;
  const activeNative = OAUTH_PROVIDER_LABEL[active] ? active : "";

  // One card per native-OAuth provider currently signed in, regardless of which one is
  // the active default (#3097) — plus the active native provider itself even when its
  // probe reports signed_out, so the failed-switch recovery card (#2460) still shows.
  const nativeProviders = [
    ...new Set([
      ...(activeNative ? [activeNative] : []),
      ...(oauth?.providers ?? [])
        .filter((p) => p.signed_in && OAUTH_PROVIDER_LABEL[p.provider])
        .map((p) => p.provider),
    ]),
  ];

  // A gateway/API-key default is a connection too — list it alongside the native ones.
  const showGateway = !activeNative && Boolean(active);
  if (nativeProviders.length === 0 && !showGateway) return null;

  // The pending-switch re-check messaging stays scoped to the active default provider.
  const pendingSwitch = Boolean(activeNative) && activeNative === saved && saved !== live && Boolean(live);
  const plural = nativeProviders.length + (showGateway ? 1 : 0) > 1;

  return (
    <div className="settings-subsection settings-subsection--lead" data-testid="oauth-account-section">
      <h2 className="panel-kicker">{plural ? "Connected accounts" : "Connected account"}</h2>
      {nativeProviders.map((provider) => (
        <div key={provider} className="oauth-account-entry" data-testid="oauth-account-entry">
          <p className="muted">
            {provider === active && pendingSwitch
              ? `Switching to your ${OAUTH_PROVIDER_LABEL[provider]} — the saved provider isn't live yet. Already signed in? Re-check completes the switch; otherwise sign in and it completes automatically.`
              : provider === active
                ? // Terse on purpose: the disconnect caveat lives on the Disconnect
                  // button's tooltip — repeating it here doubled the section's height.
                  `This agent runs on your ${OAUTH_PROVIDER_LABEL[provider]}.`
                : // A connected provider that isn't the current default — you can still
                  // manage it here without switching model.provider to it first (#3097).
                  `Your ${OAUTH_PROVIDER_LABEL[provider]} is connected but isn't the current default.`}
          </p>
          <OAuthAccountCard provider={provider} />
        </div>
      ))}
      {showGateway ? (
        <div className="oauth-account-entry" data-testid="gateway-connection">
          <p className="muted">
            This agent runs on your model gateway (API key) — manage its base URL and key in the fields below.
          </p>
        </div>
      ) : null}
    </div>
  );
}
