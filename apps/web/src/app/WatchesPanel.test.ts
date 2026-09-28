// DS 0.63 card 3b: the watch-create dialog opts into DS Dialog `padding="roomy"` so its body keeps
// the 24px padding once the app-wide `.pl-dialog__body` rule is removed by a later card. This pins
// that WatchCreateDialog keeps passing `padding="roomy"` to the DS Dialog. We spy the DS Dialog
// (capture props, render nothing) to read the prop directly rather than mount the HitlForm body,
// and stub the verifiers query so no network fetch is attempted.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type DialogProps = { padding?: string };
const dialogProps: DialogProps[] = [];

vi.mock("@protolabsai/ui/overlays", () => ({
  Dialog: (props: DialogProps) => {
    dialogProps.push(props);
    return null;
  },
  useToast: () => vi.fn(),
}));

vi.mock("../lib/queries", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/queries")>();
  return { ...actual, verifiersQuery: () => ({ queryKey: ["verifiers"], queryFn: async () => ({}) }) };
});

import { WatchCreateDialog } from "./WatchesPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  dialogProps.length = 0;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("WatchCreateDialog — DS Dialog padding (card 3b)", () => {
  it('opts the watch-create dialog into padding="roomy"', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    act(() => {
      root.render(
        h(
          QueryClientProvider,
          { client: qc },
          h(WatchCreateDialog, { open: true, onClose: () => {}, onCreate: () => {}, busy: false }),
        ),
      );
    });

    expect(dialogProps).toHaveLength(1);
    expect(dialogProps[0].padding).toBe("roomy");
  });
});
