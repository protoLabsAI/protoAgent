import { createContext, useContext } from "react";

/** The resolved host frame, including the phone's fullscreen Drawer. */
export const SurfaceFrameContext = createContext<"rail" | "modal" | "sheet" | undefined>(undefined);
export const useSurfaceFrame = () => useContext(SurfaceFrameContext);
export const useWorkActionSize = () => useSurfaceFrame() === "sheet" ? "md" as const : "sm" as const;
