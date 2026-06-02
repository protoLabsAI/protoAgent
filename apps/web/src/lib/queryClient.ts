import { QueryClient } from "@tanstack/react-query";

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
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
