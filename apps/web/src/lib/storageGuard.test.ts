import ts from "typescript";
import { describe, expect, it } from "vitest";

// Source guard for ADR 0114 D1: every browser-storage access goes through lib/storage.ts.
// A raw `localStorage.setItem` anywhere else is how a full quota crashed the console (zustand
// `persist` calls it inside `set()` with no catch). Same sourcing as app/statusTokenGuard.test.ts
// — Vite `?raw` globs (no node:fs in this tsconfig), compile-time, picking up new files for free
// — but each file is PARSED with the TypeScript compiler (a devDependency) rather than
// regex-scanned: comments, strings, template literals and regex literals are then exactly what
// they are, so neither `'a//b'` nor a `'./*.tsx'` glob can hide the line after it. The console has
// no ESLint, and a `no-restricted-properties` rule would miss `window.`/`globalThis.`/bracket
// forms anyway.
//
// What it can't see (stated, not pretended): a name BUILT at runtime (`w["local" + "Storage"]`).
// That's deliberate evasion, which review catches; the guard is for honest mistakes.
const TS_SOURCES = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// Glob keys are importer-relative (this file lives in src/lib): `./x` is src/lib/x.
const pretty = (file: string) => file.replace(/^\.\.\//, "src/").replace(/^\.\//, "src/lib/");
const isTest = (file: string) => /\.test\.tsx?$/.test(file) || /\.testkit\.tsx?$/.test(file);

// Where raw Web Storage is allowed: the seam, and the crash page (free to fall back to raw
// access if the seam ever grows imports).
const STORAGE_ALLOW = new Set(["src/lib/storage.ts", "src/app/AppCrash.tsx"]);
// The only module allowed to touch IndexedDB (ADR 0114 D2 — lands in slice 5).
const IDB_ALLOW = new Set(["src/chat/transcriptStore.ts"]);

type Rule = "storage" | "idb";
const STORAGE_NAMES = new Set(["localStorage", "sessionStorage"]);

function parse(file: string, text: string): ts.SourceFile {
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  return ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
}

function lineOf(sf: ts.SourceFile, node: ts.Node): number {
  return sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
}

/** Every banned access in one file, as `line: why`. */
export function findBanned(file: string, text: string, rule: Rule): string[] {
  const sf = parse(file, text);
  const hits: string[] = [];
  const visit = (node: ts.Node) => {
    if (rule === "storage") {
      // Any identifier (bare, `window.x`, `globalThis?.x`, destructured, shorthand …).
      if (ts.isIdentifier(node) && STORAGE_NAMES.has(node.text)) hits.push(`${lineOf(sf, node)}: ${node.text}`);
      // A literal key: `window["localStorage"]`.
      if (ts.isStringLiteralLike(node) && STORAGE_NAMES.has(node.text) && ts.isElementAccessExpression(node.parent)) {
        hits.push(`${lineOf(sf, node)}: ["${node.text}"]`);
      }
      // `Storage.prototype.setItem.call(…)` — reaching the API around the instance.
      if (
        ts.isPropertyAccessExpression(node) &&
        node.name.text === "prototype" &&
        ts.isIdentifier(node.expression) &&
        node.expression.text === "Storage"
      ) {
        hits.push(`${lineOf(sf, node)}: Storage.prototype`);
      }
    } else {
      if (ts.isIdentifier(node) && node.text === "indexedDB") hits.push(`${lineOf(sf, node)}: indexedDB`);
      if (
        (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
        node.moduleSpecifier &&
        ts.isStringLiteral(node.moduleSpecifier) &&
        node.moduleSpecifier.text === "idb"
      ) {
        hits.push(`${lineOf(sf, node)}: from "idb"`);
      }
      if (
        ts.isCallExpression(node) &&
        node.expression.kind === ts.SyntaxKind.ImportKeyword &&
        node.arguments[0] &&
        ts.isStringLiteral(node.arguments[0]) &&
        node.arguments[0].text === "idb"
      ) {
        hits.push(`${lineOf(sf, node)}: import("idb")`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return hits;
}

export function storageOffenders(sources: Record<string, string>, rule: Rule, allow: Set<string>): string[] {
  const out: string[] = [];
  for (const [file, text] of Object.entries(sources)) {
    const name = pretty(file);
    if (allow.has(name) || isTest(file)) continue;
    for (const hit of findBanned(file, text, rule)) out.push(`${name}:${hit}`);
  }
  return out;
}

/** zustand `persist(…)` calls whose options carry no `storage` (zustand then falls back to raw
 *  localStorage — the unguarded writer). Only in files importing zustand's `persist`. */
export function persistWithoutStorage(sources: Record<string, string>): string[] {
  const hits: string[] = [];
  for (const [file, text] of Object.entries(sources)) {
    if (isTest(file)) continue;
    const sf = parse(file, text);
    const local = new Set<string>(); // the local name(s) zustand's persist is imported as
    for (const st of sf.statements) {
      if (
        ts.isImportDeclaration(st) &&
        ts.isStringLiteral(st.moduleSpecifier) &&
        st.moduleSpecifier.text === "zustand/middleware" &&
        st.importClause?.namedBindings &&
        ts.isNamedImports(st.importClause.namedBindings)
      ) {
        for (const el of st.importClause.namedBindings.elements) {
          if ((el.propertyName ?? el.name).text === "persist") local.add(el.name.text);
        }
      }
    }
    if (!local.size) continue;
    const visit = (node: ts.Node) => {
      if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && local.has(node.expression.text)) {
        const opts = node.arguments[1];
        const hasStorage =
          !!opts &&
          (!ts.isObjectLiteralExpression(opts) || // a variable: can't see inside — trust it
            opts.properties.some(
              (p) =>
                ts.isSpreadAssignment(p) ||
                ((ts.isPropertyAssignment(p) || ts.isShorthandPropertyAssignment(p)) &&
                  ts.isIdentifier(p.name) &&
                  p.name.text === "storage"),
            ));
        if (!hasStorage) hits.push(`${pretty(file)}:${lineOf(sf, node)}`);
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
  }
  return hits;
}

describe("browser storage goes through lib/storage.ts (ADR 0114 D1)", () => {
  it("no raw localStorage / sessionStorage outside the seam", () => {
    expect(storageOffenders(TS_SOURCES, "storage", STORAGE_ALLOW)).toEqual([]);
  });

  it("every zustand persist( names a storage: (the seam's persistStorage)", () => {
    expect(persistWithoutStorage(TS_SOURCES)).toEqual([]);
  });

  it("no IndexedDB outside chat/transcriptStore.ts", () => {
    expect(storageOffenders(TS_SOURCES, "idb", IDB_ALLOW)).toEqual([]);
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
  const fixture = (src: string, name = "../fixture/x.ts") => ({ [name]: src });
  const hits = (src: string, name?: string) => storageOffenders(fixture(src, name), "storage", STORAGE_ALLOW);

  it("catches window., globalThis?., bracket, destructured and prototype forms", () => {
    for (const src of [
      `window.${LS}.setItem("k", v);`,
      `globalThis.${LS}?.setItem("k", v);`,
      `${LS}[k] = v;`,
      `const s = ${SS};`,
      `const { getItem } = window.${LS};`,
      `window["${LS}"].setItem("k", v);`,
      `Storage.prototype.setItem.call(w[k], "k", v);`,
    ]) {
      expect(hits(src), src).toHaveLength(1);
    }
  });

  it("can't be blinded by // or /* inside strings, templates or globs (the regex-stripper bypasses)", () => {
    expect(hits("const u = `${base}//x`; " + LS + ".setItem('k', v);")).toHaveLength(1);
    expect(hits("const p = 'a//b'; " + LS + ".setItem('k', v);")).toHaveLength(1);
    expect(hits("const m = import.meta.glob('./*.tsx');\n" + LS + ".setItem('k', v);\n/* note */")).toHaveLength(1);
    expect(hits("const r = /\\/\\//; " + LS + ".setItem('k', v);")).toHaveLength(1);
    expect(hits(`const el = <div title="a//b">{${LS}.getItem("k")}</div>;`, "../fixture/x.tsx")).toHaveLength(1);
  });

  it("ignores comments, prose strings, JSX text, allowlisted files and tests", () => {
    expect(hits(`// ${LS} is origin-keyed\n/* ${SS}\n */ const a = 1;`)).toEqual([]);
    expect(hits(`const msg = "${LS} unavailable";`)).toEqual([]);
    expect(hits(`const el = <p>${LS} is full</p>;`, "../fixture/x.tsx")).toEqual([]);
    expect(storageOffenders({ "./storage.ts": `globalThis.${LS}` }, "storage", STORAGE_ALLOW)).toEqual([]);
    expect(storageOffenders({ "../x/a.test.ts": `${LS}.clear()` }, "storage", STORAGE_ALLOW)).toEqual([]);
  });

  it("catches a zustand persist( with no storage:, passes one with it", () => {
    const imp = `import { persist } from "zustand/middleware";\n`;
    expect(persistWithoutStorage(fixture(`${imp}create()(persist((set) => ({ a: f(1) }), { name: "k" }));`))).toHaveLength(1);
    expect(persistWithoutStorage(fixture(`${imp}create()(persist((set) => ({})));`))).toHaveLength(1);
    expect(persistWithoutStorage(fixture(`${imp}create()(persist((set) => ({}), { name: "k", storage: s }));`))).toEqual([]);
    // An aliased import is still zustand's persist.
    const alias = `import { persist as keep } from "zustand/middleware";\n`;
    expect(persistWithoutStorage(fixture(`${alias}create()(keep(() => ({}), { name: "k" }));`))).toHaveLength(1);
    // A file that doesn't import zustand's persist (chat-store's own persist()) is not a hit.
    expect(persistWithoutStorage(fixture(`function persist(state) {}\npersist(x);`))).toEqual([]);
  });

  it("catches indexedDB and the idb package", () => {
    const idb = (src: string, name?: string) => storageOffenders(fixture(src, name), "idb", IDB_ALLOW);
    expect(idb(`const r = window.${"indexed" + "DB"}.open("x");`)).toHaveLength(1);
    expect(idb(`import { openDB } from ${'"i' + 'db"'};`)).toHaveLength(1);
    expect(idb(`const m = await import(${'"i' + 'db"'});`)).toHaveLength(1);
    expect(idb(`indexedDB.open("x")`, "../chat/transcriptStore.ts")).toEqual([]);
  });
});
