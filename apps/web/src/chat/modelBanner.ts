import type { RuntimeStatus } from "../lib/types";

export function needsModelBanner(runtime: Pick<RuntimeStatus, "model_configured"> | null | undefined): boolean {
  return runtime?.model_configured === false;
}
