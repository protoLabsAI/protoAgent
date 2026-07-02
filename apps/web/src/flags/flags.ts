// Developer flags (ADR 0068) — the console half. The backend (runtime/flags.py) resolves
// each flag's tier against the runtime channel and serves it at /api/flags; here we add the
// two frontend override layers (a shareable ?flag:<id>= query param, and a device-local
// panel toggle) on top, and expose `useFlag(id)` for gating console UI.
//
// Effective state precedence (ADR 0068 D3, frontend half):
//   ?flag:<id>= query param  >  device-local panel toggle  >  server channel-resolved state
// (The server has already applied the PROTOAGENT_FLAG_<ID> env override beneath that.)

import { useCallback } from "react";

import { useQuery } from "@tanstack/react-query";

import { createUISlice } from "../ext/uiStateRegistry";
import { flagsQuery } from "../lib/queries";
import type { FlagChannel } from "../lib/types";

// Device-local panel overrides — persisted per-agent (like the other UI slices), so a
// developer's toggles never leak into shared config. `undefined` = follow the server state.
type OverrideState = { overrides: Record<string, boolean> };
export const useFlagOverrides = createUISlice<OverrideState>("dev-flags", { overrides: {} });

export function setFlagOverride(id: string, on: boolean): void {
  useFlagOverrides.setState((s) => ({ overrides: { ...s.overrides, [id]: on } }));
}
export function clearFlagOverride(id: string): void {
  useFlagOverrides.setState((s) => {
    const next = { ...s.overrides };
    delete next[id];
    return { overrides: next };
  });
}
export function resetFlagOverrides(): void {
  useFlagOverrides.setState({ overrides: {} });
}

// Transient, shareable overrides parsed ONCE from ?flag:<id>=on|off (a "try this build"
// link). Not persisted — they last only for the current page load.
const TRUEY = new Set(["1", "on", "true", "yes"]);
const _queryOverrides: Record<string, boolean> = (() => {
  const out: Record<string, boolean> = {};
  try {
    new URLSearchParams(window.location.search).forEach((value, key) => {
      if (key.startsWith("flag:")) out[key.slice(5)] = TRUEY.has(value.trim().toLowerCase());
    });
  } catch {
    /* no location (SSR/tests) — no query overrides */
  }
  return out;
})();

export function useFlags() {
  return useQuery(flagsQuery());
}

/** A predicate over ANY flag id, same precedence as `useFlag` — for gating LISTS (e.g.
 *  flag-tagged slash commands) where calling a per-id hook in a loop is illegal. Stable
 *  per (server state, overrides) so it's safe in memo deps. Fail-closed like `useFlag`. */
export function useFlagPredicate(): (id: string) => boolean {
  const { data } = useFlags();
  const overrides = useFlagOverrides((s) => s.overrides);
  return useCallback(
    (id: string) => {
      if (id in _queryOverrides) return _queryOverrides[id];
      const override = overrides[id];
      if (override !== undefined) return override;
      return data?.flags.find((f) => f.id === id)?.enabled ?? false;
    },
    [data, overrides],
  );
}

/** Is a developer flag ON for this session? Fail-closed: an unknown/loading flag is off. */
export function useFlag(id: string): boolean {
  return useFlagPredicate()(id);
}

/** The runtime channel this instance runs on (prod | beta | dev). */
export function useDeveloperChannel(): FlagChannel {
  return useFlags().data?.channel ?? "prod";
}

/** Whether to surface the Developer panel: a dev build, a non-prod channel, or an explicit
 *  `?dev` / `?flag:` reveal — so production users never see it, but a developer always can. */
export function developerPanelVisible(channel: FlagChannel): boolean {
  let revealed = false;
  try {
    const q = new URLSearchParams(window.location.search);
    revealed = q.has("dev") || [...q.keys()].some((k) => k.startsWith("flag:"));
  } catch {
    /* no location */
  }
  return import.meta.env.DEV || channel !== "prod" || revealed;
}
