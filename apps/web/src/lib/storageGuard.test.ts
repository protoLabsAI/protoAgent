import { describe, expect, it } from "vitest";

// Source guard for ADR 0114 D1: every browser-storage access goes through lib/storage.ts.
// A raw `localStorage.setItem` anywhere else is how a full quota crashed the console (zustand
// `persist` calls it inside `set()` with no catch). Same style as app/statusTokenGuard.test.ts:
// Vite `?raw` globs (no node:fs in this tsconfig), compile-time, picking up new files for free.
// The console has no ESLint, and a `no-restricted-properties` rule would miss the
// `window.` / `globalThis.` / optional-chain / bracket forms anyway.
const TS_SOURCES = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/lib): `./x` is src/lib/x.
const pretty = (file: string) => file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/lib/");

const isTest = (file: string) => /\.test\.tsx?$/.test(file) || /\/test-utils?\//.test(file);

// Where raw Web Storage is allowed: the seam itself, and the crash page (which today goes
// through the seam too, but must be free to fall back to raw access if the seam ever grows
// imports).
const STORAGE_ALLOW = new Set(["src/lib/storage.ts", "src/app/AppCrash.tsx"]);
// The only module allowed to touch IndexedDB (ADR 0114 D2 — lands in slice 5).
const IDB_ALLOW = new Set(["src/chat/transcriptStore.ts"]);

export const RAW_STORAGE = /\b(localStorage|sessionStorage)\b/;
export const RAW_IDB = /\bindexedDB\b|from\s+["']idb["']/;

/** Blank out comments (so prose that mentions the APIs never trips the guard), keeping line
 *  numbers. `//` only counts when it can't be inside a URL string (`http://…`). */
export function stripComments(src: string): string {
  const noBlock = src.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));
  return noBlock
    .split("\n")
    .map((line) => line.replace(/(^|[^:"'`\\])\/\/.*$/, "$1"))
    .join("\n");
}

export function storageOffenders(sources: Record<string, string>, pattern: RegExp, allow: Set<string>): string[] {
  const hits: string[] = [];
  for (const [file, text] of Object.entries(sources)) {
    const name = pretty(file);
    if (allow.has(name) || isTest(file)) continue;
    stripComments(text)
      .split("\n")
      .forEach((line, i) => {
        if (pattern.test(line)) hits.push(`${name}:${i + 1}: ${line.trim()}`);
      });
  }
  return hits;
}

/** zustand `persist(` calls with no `storage:` option (which falls back to raw
 *  localStorage — the unguarded writer). Only in files that import zustand's persist. */
export function persistWithoutStorage(sources: Record<string, string>): string[] {
  const hits: string[] = [];
  for (const [file, text] of Object.entries(sources)) {
    if (isTest(file)) continue;
    const code = stripComments(text);
    if (!/import\s*{[^}]*\bpersist\b[^}]*}\s*from\s*["']zustand\/middleware["']/.test(code)) continue;
    const re = /\bpersist\s*\(/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(code))) {
      // Walk to the matching close paren; the call's full text must carry `storage:`.
      let depth = 0;
      let end = m.index + m[0].length - 1;
      for (; end < code.length; end++) {
        if (code[end] === "(") depth++;
        else if (code[end] === ")" && --depth === 0) break;
      }
      const call = code.slice(m.index, end + 1);
      if (!/\bstorage\s*:/.test(call)) {
        hits.push(`${pretty(file)}:${code.slice(0, m.index).split("\n").length}`);
      }
    }
  }
  return hits;
}

describe("browser storage goes through lib/storage.ts (ADR 0114 D1)", () => {
  it("no raw localStorage / sessionStorage outside the seam", () => {
    expect(storageOffenders(TS_SOURCES, RAW_STORAGE, STORAGE_ALLOW)).toEqual([]);
  });

  it("every zustand persist( names a storage: (the seam's persistStorage)", () => {
    expect(persistWithoutStorage(TS_SOURCES)).toEqual([]);
  });

  it("no IndexedDB outside chat/transcriptStore.ts", () => {
    expect(storageOffenders(TS_SOURCES, RAW_IDB, IDB_ALLOW)).toEqual([]);
  });

  it("actually covers the tree (a glob typo can't pass an empty sweep)", () => {
    const files = Object.keys(TS_SOURCES).map(pretty);
    for (const known of ["src/lib/storage.ts", "src/state/uiStore.ts", "src/chat/chat-store.ts", "src/app/App.tsx"]) {
      expect(files).toContain(known);
    }
    expect(files.length).toBeGreaterThan(100);
  });
});

describe("the guard itself still bites (self-test)", () => {
  const LS = "local" + "Storage";
  const SS = "session" + "Storage";
  const fixture = (src: string) => ({ "../fixture/x.ts": src });

  it("catches window., globalThis?. and bare bracket forms", () => {
    for (const src of [
      `window.${LS}.setItem("k", v);`,
      `globalThis.${LS}?.setItem("k", v);`,
      `${LS}[k] = v;`,
      `const s = ${SS};`,
      `const { getItem } = window.${LS};`,
    ]) {
      expect(storageOffenders(fixture(src), RAW_STORAGE, STORAGE_ALLOW), src).toHaveLength(1);
    }
  });

  it("ignores comments and allowlisted files, but not strings after a URL", () => {
    expect(storageOffenders(fixture(`// ${LS} is origin-keyed\n/* ${SS}\n */`), RAW_STORAGE, STORAGE_ALLOW)).toEqual([]);
    expect(storageOffenders({ "./storage.ts": `globalThis.${LS}` }, RAW_STORAGE, STORAGE_ALLOW)).toEqual([]);
    expect(storageOffenders({ "../x/a.test.ts": `${LS}.clear()` }, RAW_STORAGE, STORAGE_ALLOW)).toEqual([]);
    expect(
      storageOffenders(fixture(`fetch("http://x"); ${LS}.setItem("k", "v");`), RAW_STORAGE, STORAGE_ALLOW),
    ).toHaveLength(1);
  });

  it("catches a zustand persist( with no storage:, passes one with it", () => {
    const imp = `import { persist } from "zustand/middleware";\n`;
    expect(persistWithoutStorage(fixture(`${imp}create()(persist((set) => ({ a: f(1) }), { name: "k" }));`))).toHaveLength(1);
    expect(
      persistWithoutStorage(fixture(`${imp}create()(persist((set) => ({}), { name: "k", storage: s }));`)),
    ).toEqual([]);
    // A file that doesn't import zustand's persist (chat-store's own persist()) is not a hit.
    expect(persistWithoutStorage(fixture(`function persist(state) {}\npersist(x);`))).toEqual([]);
  });

  it("catches indexedDB and the idb package", () => {
    expect(storageOffenders(fixture(`const r = window.${"indexed" + "DB"}.open("x");`), RAW_IDB, IDB_ALLOW)).toHaveLength(1);
    expect(storageOffenders(fixture(`import { openDB } from ${'"i' + 'db"'};`), RAW_IDB, IDB_ALLOW)).toHaveLength(1);
    expect(storageOffenders({ "../chat/transcriptStore.ts": `indexedDB.open("x")` }, RAW_IDB, IDB_ALLOW)).toEqual([]);
  });
});
