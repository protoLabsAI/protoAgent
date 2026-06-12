import { QueryClient } from "@tanstack/react-query";

import { is401, isColdStart } from "./api";

// One QueryClient for the whole console (ADR 0013). Surfaces fetch with
// `useSuspenseQuery` so loading is a <Suspense> fallback and errors are caught
// by an <ErrorBoundary> — replacing the per-surface useEffect + busy-flag +
// try/catch→setError plumbing.
//
// Defaults: data is fresh for 5s (avoids refetch storms when surfaces remount
// as you switch tabs), one retry on failure, and no refetch-on-focus (a local
// operator console, not a long-lived dashboard). Individual queries opt into
// `refetchInterval` for live surfaces (e.g. goals the agent advances mid-turn).
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      // Cold start (ADR 0042): switching to a not-yet-running fleet agent makes its
      // panels answer 409 (member spawning) / 502 (booting, not bound) for a few
      // seconds. Without this they gave up after one retry and flashed an error
      // mid-boot; ride out cold-start codes — up to ~25 retries with capped backoff
      // (~70s, covering a slow first launch and outlasting the boot probe's window) —
      // so a panel stays in its loading state until the agent is up. Everything else
      // keeps the single retry. (Queries that opt out via `retry: false` are unaffected;
      // a genuinely-down agent still surfaces via the shell's boot-gate "isn't responding".)
      // 401 never retries: the answer can't change until the operator enters a
      // token — the AuthGate prompt (#873) owns recovery (it invalidates every
      // query after the token is saved).
      retry: (failureCount, error) => {
        if (is401(error)) return false;
        return isColdStart(error) ? failureCount < 25 : failureCount < 1;
      },
      retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 3000),
      refetchOnWindowFocus: false,
    },
  },
});
