import { useEffect, useState } from "react";

// True below the mobile breakpoint (ADR 0035 S4). Drives the single-surface mobile shell vs the
// desktop dual-rail split. SSR-safe (defaults to false until mounted).
const QUERY = "(max-width: 767px)";
export function useIsMobile(): boolean {
  const [mobile, setMobile] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia(QUERY);
    const on = () => setMobile(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return mobile;
}
