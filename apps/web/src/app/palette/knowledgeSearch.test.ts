// The ⌘K knowledge provider, tested against the DS `CommandProvider` contract it is written
// to (`getCommands(query, { signal })` — debounced and aborted by the commands view, its
// results appended to the palette VERBATIM, its rejections swallowed by `Promise.allSettled`).
//
// Four of these are guards on facts about the ENDPOINT that a naive provider gets wrong, and
// each one fails as a UX bug rather than an exception:
//   * an empty/short `q` is a BROWSE default server-side (the most recent chunks), so an
//     unguarded provider floods the palette root the moment ⌘K opens;
//   * `k` is unclamped on that route, so the caller is the only ceiling;
//   * a rejected provider is indistinguishable from "no matches" in the DS loop, so a failure
//     that resolves to zero rows is a silent lie;
//   * an ABORT is not a failure — it is the next keystroke — so it must not surface that row.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../lib/api";
import type { KnowledgeChunk } from "../../lib/types";
import type { NavIntent } from "../usePaletteRegistry";
import {
  KNOWLEDGE_GROUP,
  KNOWLEDGE_PROVIDER_ID,
  KNOWLEDGE_RESULT_CAP,
  KNOWLEDGE_TIMEOUT_MS,
  knowledgeRowLabel,
  knowledgeSearchProvider,
} from "./knowledgeSearch";

type SearchOpts = { k?: number; signal?: AbortSignal };

function chunk(over: Partial<KnowledgeChunk> = {}): KnowledgeChunk {
  return {
    id: 7,
    heading: "Postgres tuning",
    content: "shared_buffers should be a quarter of RAM",
    preview: "shared_buffers should be a quarter of RAM",
    domain: "infra",
    source: "runbook.md",
    source_type: "document",
    finding_type: null,
    created_at: "2026-08-28T10:00:00+00:00",
    ...over,
  };
}

const ok = (results: KnowledgeChunk[]) => ({ enabled: true, query: "q", results, stats: {} });

/** The palette always hands a live signal; a test that forgets one would exercise a path the
 *  console never takes. */
const read = (
  provider: ReturnType<typeof knowledgeSearchProvider>,
  query: string,
  signal = new AbortController().signal,
) => Promise.resolve(provider.getCommands!(query, { signal }));

const intents: NavIntent[] = [];
const navigate = (intent: NavIntent) => intents.push(intent);
const provider = () => knowledgeSearchProvider(navigate);

beforeEach(() => {
  intents.length = 0;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("knowledgeSearchProvider — the browse-default guard", () => {
  it("does not call the endpoint at all for an empty, blank, or one-character query", async () => {
    const search = vi.spyOn(api, "knowledgeSearch");
    for (const q of ["", "   ", "p", " p "]) {
      expect(await read(provider(), q)).toEqual([]);
    }
    // The point: `/api/knowledge/search?q=` answers with the 30 most-recent chunks. Reaching
    // the endpoint at all here would paste them under the palette's own commands on open.
    expect(search).not.toHaveBeenCalled();
  });

  it("searches once the query is long enough", async () => {
    const search = vi.spyOn(api, "knowledgeSearch").mockResolvedValue(ok([chunk()]));
    const rows = await read(provider(), "  postgres  ");
    expect(search).toHaveBeenCalledTimes(1);
    expect(search.mock.calls[0][0]).toBe("postgres"); // trimmed, not the raw box contents
    expect(rows.map((r) => r.id)).toEqual(["knowledge:7"]);
  });
});

describe("knowledgeSearchProvider — the request it makes", () => {
  it("sends an explicit small k, because the route does not clamp it", async () => {
    const search = vi.spyOn(api, "knowledgeSearch").mockResolvedValue(ok([]));
    await read(provider(), "postgres");
    expect((search.mock.calls[0][1] as SearchOpts).k).toBe(KNOWLEDGE_RESULT_CAP);
  });

  it("threads the palette's abort signal into the in-flight request", async () => {
    let passed: AbortSignal | undefined;
    vi.spyOn(api, "knowledgeSearch").mockImplementation(
      (_q: string, opts?: SearchOpts) =>
        new Promise((_resolve, reject) => {
          passed = opts?.signal;
          opts?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    );
    const outer = new AbortController();
    const pending = read(provider(), "postgres", outer.signal);
    expect(passed).toBeInstanceOf(AbortSignal);
    expect(passed!.aborted).toBe(false);
    // A superseded keystroke has to CANCEL the request, not merely have its answer thrown
    // away — that is the whole reason the DS's signal is threaded rather than dropped.
    outer.abort();
    expect(passed!.aborted).toBe(true);
    await pending;
  });

  it("caps the rows even when the backend ignores k", async () => {
    const many = Array.from({ length: 40 }, (_, i) => chunk({ id: i + 1, heading: `row ${i}` }));
    vi.spyOn(api, "knowledgeSearch").mockResolvedValue(ok(many));
    const rows = await read(provider(), "postgres");
    expect(rows).toHaveLength(KNOWLEDGE_RESULT_CAP);
  });
});

describe("knowledgeSearchProvider — the rows", () => {
  it("namespaces every id so a same-id static cannot swallow the result", async () => {
    vi.spyOn(api, "knowledgeSearch").mockResolvedValue(ok([chunk({ id: 12 })]));
    const [row] = await read(provider(), "postgres");
    // The DS dedups FIRST-WINS with statics listed ahead of provider rows: a bare "12" (or
    // "settings", or "open") would be silently dropped by whatever claimed it first.
    expect(row.id).toBe("knowledge:12");
    expect(row.group).toBe(KNOWLEDGE_GROUP);
    expect(row.label).toBe("Postgres tuning");
    expect(row.hint).toBe("infra");
    expect(row.keywords).toContain("knowledge");
    expect(row.keywords).toContain("infra");
    expect(row.disabled).toBeFalsy();
  });

  it("labels an untitled chunk from its text, and never renders an empty row", () => {
    expect(knowledgeRowLabel(chunk({ heading: "  " }))).toBe(
      "shared_buffers should be a quarter of RAM",
    );
    expect(knowledgeRowLabel(chunk({ heading: "", preview: "", content: "" }))).toBe("Chunk 7");
    const long = knowledgeRowLabel(chunk({ heading: "x".repeat(200) }));
    expect(long.length).toBeLessThanOrEqual(72);
    expect(long.endsWith("…")).toBe(true);
    // A chunk is a paragraph — newlines would break the single-line row.
    expect(knowledgeRowLabel(chunk({ heading: "a\n\n  b" }))).toBe("a b");
  });

  it("opens through the injected navigator, carrying the query, and closes the palette", async () => {
    vi.spyOn(api, "knowledgeSearch").mockResolvedValue(ok([chunk()]));
    const [row] = await read(provider(), "postgres");
    let closed = false;
    row.run({ enter: () => {}, back: () => {}, close: () => (closed = true), props: undefined });
    // NOT `useUI.getState()`: the desktop launcher mounts this registry in a shell-less
    // context where a direct store write is an inert no-op, and its navigator forwards this
    // serializable intent to the real console window instead.
    expect(intents).toEqual([{ kind: "knowledge", query: "postgres" }]);
    expect(closed).toBe(true);
  });
});

describe("knowledgeSearchProvider — failure is named, not silent", () => {
  it("resolves a failed search to a disabled row that says so", async () => {
    vi.spyOn(api, "knowledgeSearch").mockRejectedValue(new Error("500 knowledge store offline"));
    const rows = await read(provider(), "postgres");
    // Rejecting would be indistinguishable from "no matches": the DS's `Promise.allSettled`
    // keeps only fulfilled providers, so a thrown search renders as an empty result set.
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe("knowledge:unavailable");
    expect(rows[0].label).toBe("Knowledge search unavailable");
    expect(rows[0].hint).toBe("500 knowledge store offline");
    expect(rows[0].disabled).toBe(true);
  });

  it("stays silent when the instance simply has no knowledge store", async () => {
    vi.spyOn(api, "knowledgeSearch").mockResolvedValue({
      enabled: false,
      query: "postgres",
      results: [],
      stats: {},
    });
    // Nothing to search is not a failure — an error row under every keystroke would be noise.
    expect(await read(provider(), "postgres")).toEqual([]);
  });

  it("treats an abort as the next keystroke, not as a failure", async () => {
    const outer = new AbortController();
    vi.spyOn(api, "knowledgeSearch").mockImplementation(
      (_q: string, opts?: SearchOpts) =>
        new Promise((_resolve, reject) => {
          opts?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    );
    const pending = read(provider(), "postgres", outer.signal);
    outer.abort();
    expect(await pending).toEqual([]); // no "unavailable" row for a query the operator replaced
  });

  it("gives up on a hung search, cancels it, and says it timed out", async () => {
    vi.useFakeTimers();
    let inner: AbortSignal | undefined;
    vi.spyOn(api, "knowledgeSearch").mockImplementation(
      (_q: string, opts?: SearchOpts) =>
        new Promise((_resolve, reject) => {
          inner = opts?.signal;
          opts?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    );
    const pending = read(provider(), "postgres");
    await vi.advanceTimersByTimeAsync(KNOWLEDGE_TIMEOUT_MS);
    const rows = await pending;
    // A hybrid store embeds the query over HTTP first; without a ceiling the palette shows
    // "Searching…" forever. The deadline CANCELS the request rather than ignoring its answer.
    expect(inner!.aborted).toBe(true);
    expect(rows).toHaveLength(1);
    expect(rows[0].hint).toBe("timed out");
    expect(rows[0].disabled).toBe(true);
  });
});

describe("api.knowledgeSearch — the wire the provider needs", () => {
  it("puts k on the query string and hands the signal to fetch", async () => {
    let url = "";
    let init: RequestInit | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (u: string | URL | Request, i?: RequestInit) => {
        url = String(u);
        init = i;
        return new Response(JSON.stringify(ok([])), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    const ac = new AbortController();
    await api.knowledgeSearch("postgres", { k: 6, signal: ac.signal });
    vi.unstubAllGlobals();
    expect(url).toContain("q=postgres");
    expect(url).toContain("k=6");
    expect(init?.signal).toBe(ac.signal);
  });

  it("omits k entirely when the caller does not ask, keeping the server default", async () => {
    let url = "";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (u: string | URL | Request) => {
        url = String(u);
        return new Response(JSON.stringify(ok([])), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    await api.knowledgeSearch("postgres");
    vi.unstubAllGlobals();
    // The Knowledge surface is the other caller; adding a k it never asked for would silently
    // shrink its listing from thirty rows to the palette's shortlist.
    expect(url).not.toContain("k=");
  });
});

describe("knowledgeSearchProvider — shape", () => {
  it("declares getCommands under a stable id", () => {
    const p = provider();
    expect(p.id).toBe(KNOWLEDGE_PROVIDER_ID);
    expect(typeof p.getCommands).toBe("function");
  });
});
