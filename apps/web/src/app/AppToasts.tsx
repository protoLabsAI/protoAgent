import { ToastProvider } from "@protolabsai/ui/overlays";
import { useLayoutEffect, useRef, type ReactNode } from "react";

/** Bridge the rendered AppShell rails to the global notification viewport. */
export function AppToasts({ children }: { children: ReactNode }) {
  const host = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const element = host.current!;
    const rails = new Set<Element>();
    const measure = () => {
      let left = 0;
      let right = 0;
      for (const rail of rails) {
        const rect = rail.getBoundingClientRect();
        if (!rect.width || !rect.height) continue;
        if (!rail.classList.contains("pl-rail--right")) left = Math.max(left, rect.right);
        else right = Math.max(right, document.documentElement.clientWidth - rect.left);
      }
      element.style.setProperty("--app-toast-left", `${left}px`);
      element.style.setProperty("--app-toast-right", `${right}px`);
    };
    const resize = new ResizeObserver(measure);
    const discover = () => {
      const current = new Set(element.querySelectorAll(".app-shell-main .pl-rail:not(.pl-rail--bottom)"));
      for (const rail of rails) if (!current.has(rail)) {
        resize.unobserve(rail);
        rails.delete(rail);
      }
      for (const rail of current) if (!rails.has(rail)) {
        rails.add(rail);
        resize.observe(rail);
      }
      measure();
    };
    // Shells mount after auth/setup and switch completely on phone breakpoints.
    const mutations = new MutationObserver(discover);
    mutations.observe(element, { childList: true, subtree: true });
    window.addEventListener("resize", measure);
    discover();
    return () => {
      mutations.disconnect();
      resize.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);
  return <div className="app-toast-layout" ref={host}>
    <ToastProvider position="top-right">{children}</ToastProvider>
  </div>;
}
