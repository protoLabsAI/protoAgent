/// <reference types="vite/client" />

// Module Federation runtime helpers injected by @originjs/vite-plugin-federation (ADR 0034).
// Used by FederatedView to register + load a `ui: react` plugin remote at runtime.
declare module "virtual:__federation__" {
  interface RemoteConfig {
    url: (() => Promise<string> | string) | string;
    format?: "esm" | "systemjs" | "var";
    from?: "vite" | "webpack";
  }
  export function __federation_method_setRemote(name: string, config: RemoteConfig): void;
  export function __federation_method_getRemote(name: string, exposedPath: string): Promise<unknown>;
  export function __federation_method_unwrapDefault(module: unknown): Promise<unknown>;
  export function __federation_method_ensure(name: string): Promise<unknown>;
}
