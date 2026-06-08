// Host bridge (ADR 0034 D4). The host injects this once at startup; a `ui: react` remote reads it
// via getHostBridge() to make authed API calls + read host context, without importing host
// internals. The SDK is a federation singleton, so there's one bridge across host + remotes.
export interface HostBridge {
  // The host's API client (authed fetch wrapper). Typed loosely so the SDK doesn't depend on the
  // host's concrete shape; remotes cast to the documented surface.
  api: unknown;
  authToken: () => string;
  apiUrl: (path: string) => string;
  brandName: string;
}

let bridge: HostBridge | null = null;

export function setHostBridge(b: HostBridge): void {
  bridge = b;
}

export function getHostBridge(): HostBridge {
  if (!bridge) {
    throw new Error(
      "@protoagent/plugin-ui: host bridge not set — the console host must call setHostBridge() at startup.",
    );
  }
  return bridge;
}
