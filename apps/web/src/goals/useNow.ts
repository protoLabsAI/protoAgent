import { useEffect, useState } from "react";

/** Wall-clock ms that re-renders every `intervalMs` — so "4m ago" keeps counting and a
 *  finished goal leaves the recent window without waiting for an unrelated refetch. */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
