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
import { api } from "../lib/api";
import { ApiError } from "../lib/api/http";
import { flagsQuery, queryKeys } from "../lib/queries";
import { queryClient } from "../lib/queryClient";
import type { FlagChannel, FlagsPayload } from "../lib/types";

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

// ── The panel's toggle: stored on the AGENT, so server-gated flags really turn on ────────
// A device-local override only changes what this console draws. A flag that gates server
// behaviour (a route that answers 404 while off, say) needs the server to know, so the
// panel writes the override to the agent (`PUT /api/flags/<id>`). The device-local layer
// stays as the fallback for an agent too old to store one.

/** The server answered: take its list, drop the device-local stand-in, and refetch
 *  everything else, because data gated by the flag may exist now (or be gone). */
function applied(data: FlagsPayload, id?: string): void {
  queryClient.setQueryData(queryKeys.flags, data);
  if (id) clearFlagOverride(id);
  else resetFlagOverrides();
  void queryClient.invalidateQueries();
}

/** True when the agent has no stored-override route (older than this feature). */
function unsupported(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 404 || err.status === 405) && !/no developer flag/i.test(err.message);
}

/** Turn a flag on or off. Stored on the agent when it supports that; on an older agent the
 *  change is kept on this device only. Rejects with the agent's reason when it refuses
 *  (a flag pinned by its environment variable). */
export async function saveFlag(id: string, on: boolean): Promise<"agent" | "device"> {
  try {
    applied(await api.setFlag(id, on), id);
    return "agent";
  } catch (err) {
    if (!unsupported(err)) throw err;
    setFlagOverride(id, on);
    return "device";
  }
}

/** Back to the channel default for one flag: the agent's stored override and this device's. */
export async function resetFlag(id: string): Promise<void> {
  clearFlagOverride(id);
  try {
    applied(await api.clearFlag(id), id);
  } catch (err) {
    if (!unsupported(err)) throw err;
  }
}

/** Back to the channel defaults for every flag. */
export async function resetAllFlags(): Promise<void> {
  resetFlagOverrides();
  try {
    applied(await api.clearFlag());
  } catch (err) {
    if (!unsupported(err)) throw err;
  }
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
