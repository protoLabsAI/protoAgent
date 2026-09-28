// DS 0.63 card 3b: the persona version-history dialog opts into DS Dialog `padding="roomy"` so
// its body keeps the 24px padding once the app-wide `.pl-dialog__body` rule is removed by a later
// card. This pins that the component keeps passing `padding="roomy"` to the DS Dialog. We spy the
// DS Dialog (capture props, render nothing) to read the prop directly rather than drive the full
// soul-history query/list flow.
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

vi.mock("../lib/api", () => ({
  api: { soulHistory: vi.fn().mockResolvedValue({ versions: [] }) },
}));

import { SoulHistoryButton } from "./SoulHistory";

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

describe("SoulHistoryButton — DS Dialog padding (card 3b)", () => {
  it('opts the persona-history dialog into padding="roomy"', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    act(() => {
      root.render(h(QueryClientProvider, { client: qc }, h(SoulHistoryButton, { onRestored: () => {} })));
    });

    expect(dialogProps).toHaveLength(1);
    expect(dialogProps[0].padding).toBe("roomy");
  });
});
