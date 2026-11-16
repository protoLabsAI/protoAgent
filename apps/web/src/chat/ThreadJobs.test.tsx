// ThreadJobs — the reader's "Jobs from this thread" line (other-conversations.md §11.1, C10a).
// Covers acceptance criteria 1–7: the empty states, the collapsed summary (count + "needs you"),
// expand/collapse, the job rows (order, status words, accessible names, opening Work), the Work
// tasks-query override, and that a fresh mount starts collapsed. Rendered for real with React so
// the aria wiring and DOM are exercised; the Work tasks cache is seeded through a QueryClient.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ThreadJobs } from "./ThreadJobs";
import { queryKeys } from "../lib/queries";
import { useUI } from "../state/uiStore";
import type { ConversationRow } from "../lib/api/chat";
import type { Task } from "../lib/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Snap = NonNullable<ConversationRow["tasks"]>[number];

function row(tasks?: Snap[]): ConversationRow {
  return {
    session_id: "ctx-1",
    title: "Triage",
    origin_label: "From Claude Code for Josh",
    group: "for_you",
    last_updated: null,
    turn_count: 1,
    active: false,
    last_state: null,
    ...(tasks ? { tasks } : {}),
  };
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

/** Mount ThreadJobs, optionally seeding the Work tasks query cache first (criterion 6). */
function mount(r: ConversationRow, seed?: Task[]) {
  const qc = new QueryClient();
  if (seed) qc.setQueryData(queryKeys.tasks, { issues: seed });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root!.render(
      <QueryClientProvider client={qc}>
        <ThreadJobs row={r} />
      </QueryClientProvider>,
    );
  });
  return container;
}

function click(node: Element) {
  act(() => {
    node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

const summary = () => container!.querySelector<HTMLButtonElement>(".thread-jobs__summary")!;
const rows = () => [...container!.querySelectorAll<HTMLButtonElement>(".thread-jobs__row")];

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  container = null;
  root = null;
  useUI.setState({ surfaceModal: null, surfaceStates: {} });
  vi.restoreAllMocks();
});

describe("ThreadJobs", () => {
  // Criterion 1.
  it("renders nothing when there are no open jobs", () => {
    expect(mount(row()).querySelector(".thread-jobs")).toBeNull(); // tasks missing
    act(() => root!.unmount());
    expect(mount(row([])).querySelector(".thread-jobs")).toBeNull(); // tasks empty
    act(() => root!.unmount());
    expect(
      mount(row([{ id: "c", title: "Closed one", status: "closed" }])).querySelector(".thread-jobs"),
    ).toBeNull(); // only closed
  });

  // Criterion 2.
  it("shows the collapsed summary with count and 'needs you' for 2 open jobs (one waiting)", () => {
    mount(
      row([
        { id: "a", title: "In progress job", status: "in_progress" },
        { id: "b", title: "Waiting job", status: "waiting" },
      ]),
    );
    const btn = summary();
    expect(btn.textContent).toBe("Jobs from this thread · 2 · 1 needs you");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(btn.getAttribute("aria-label")).toBe("Jobs from this thread, 2, 1 needs you");
    // The "needs you" marker is a warning StatusDot.
    expect(btn.querySelector(".thread-jobs__needs .pl-dot--warning")).not.toBeNull();
  });

  // Criterion 3.
  it("omits the 'needs you' part and warning dot when nothing is waiting", () => {
    mount(
      row([
        { id: "a", title: "In progress job", status: "in_progress" },
        { id: "b", title: "Blocked job", status: "blocked" },
        { id: "c", title: "Not started job", status: "open" },
      ]),
    );
    const btn = summary();
    expect(btn.textContent).toBe("Jobs from this thread · 3");
    expect(btn.querySelector(".pl-dot--warning")).toBeNull();
  });

  // Criterion 4 + 7: starts collapsed, expands to an ordered list named by aria-controls, collapses again.
  it("expands into an ordered list and collapses on a second activation", () => {
    mount(
      row([
        { id: "a", title: "A not started", status: "open" },
        { id: "b", title: "B blocked", status: "blocked" },
        { id: "c", title: "C waiting", status: "waiting" },
        { id: "d", title: "D in progress", status: "in_progress" },
      ]),
    );
    const btn = summary();
    // Criterion 7: a fresh mount is collapsed — no list.
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(container!.querySelector(".thread-jobs__list")).toBeNull();

    click(btn);
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    const list = container!.querySelector<HTMLUListElement>(".thread-jobs__list")!;
    expect(list).not.toBeNull();
    // aria-controls names the list.
    expect(btn.getAttribute("aria-controls")).toBe(list.id);

    // Ordered waiting, in progress, blocked, not started; each row shows a dot + title + word.
    const shown = rows().map((r) => ({
      title: r.querySelector(".thread-jobs__row-title")!.textContent,
      word: r.querySelector(".thread-jobs__row-status")!.textContent,
      dot: !!r.querySelector(".pl-dot"),
    }));
    expect(shown).toEqual([
      { title: "C waiting", word: "waiting on you", dot: true },
      { title: "D in progress", word: "in progress", dot: true },
      { title: "B blocked", word: "blocked", dot: true },
      { title: "A not started", word: "not started", dot: true },
    ]);

    click(btn);
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(container!.querySelector(".thread-jobs__list")).toBeNull();
  });

  // Criterion 5.
  it("names each row and opens its Work detail on activation", () => {
    const openSpy = vi.spyOn(useUI.getState(), "openSurfaceModal").mockImplementation(() => {});
    mount(row([{ id: "x7", title: "Release the thing", status: "in_progress" }]));
    click(summary());
    const r = rows()[0];
    expect(r.getAttribute("aria-label")).toBe("Open job: Release the thing, in progress");
    click(r);
    expect(openSpy).toHaveBeenCalledWith("work", {
      section: "tasks",
      itemId: "task:x7",
      filters: { thisChat: false },
    });
  });

  // Criterion 6: the Work tasks query overrides the (stale) row snapshot — status, word, count,
  // "needs you" and title — and a job the query reports closed is dropped.
  it("uses the Work tasks query's status/title and drops a query-closed job", () => {
    mount(
      row([
        { id: "t1", title: "Stale title", status: "open" }, // query says waiting + renamed
        { id: "t2", title: "Was in progress", status: "in_progress" }, // query says closed → gone
      ]),
      [
        { id: "t1", title: "Fresh title", status: "waiting" },
        { id: "t2", title: "Was in progress", status: "closed" },
      ],
    );
    const btn = summary();
    expect(btn.textContent).toBe("Jobs from this thread · 1 · 1 needs you");
    click(btn);
    const only = rows();
    expect(only).toHaveLength(1);
    expect(only[0].getAttribute("aria-label")).toBe("Open job: Fresh title, waiting on you");
  });
});
