// Split a multi-file unified diff (GET /api/fs/diff's `patch`, ADR 0112) into one patch per
// file, so the Diff tab can render the file the operator picked instead of the whole tree.
// Pure and forgiving: anything that isn't a `diff --git` section is ignored, never thrown on.

export type PatchFile = {
  /** The file's path after the change (its old path for a deletion). */
  path: string;
  /** Set on a rename. */
  oldPath?: string;
  /** This file's section of the patch, `diff --git` line included. */
  patch: string;
  additions: number;
  deletions: number;
  binary: boolean;
};

const unquote = (p: string) => {
  // git C-quotes paths with unusual bytes: "a/sp ace\\303\\251.txt". Strip the quotes and the
  // simple escapes; octal UTF-8 sequences are decoded best-effort.
  if (!(p.startsWith('"') && p.endsWith('"'))) return p;
  const body = p.slice(1, -1);
  const bytes: number[] = [];
  for (let i = 0; i < body.length; i++) {
    const c = body[i];
    if (c === "\\" && i + 1 < body.length) {
      const oct = body.slice(i + 1, i + 4);
      if (/^[0-7]{3}$/.test(oct)) {
        bytes.push(parseInt(oct, 8));
        i += 3;
        continue;
      }
      const esc: Record<string, string> = { n: "\n", t: "\t", '"': '"', "\\": "\\" };
      const ch = esc[body[i + 1]] ?? body[i + 1];
      bytes.push(...new TextEncoder().encode(ch));
      i += 1;
      continue;
    }
    bytes.push(...new TextEncoder().encode(c));
  }
  return new TextDecoder().decode(new Uint8Array(bytes));
};

const stripPrefix = (p: string) => {
  const u = unquote(p.trim());
  return u.startsWith("a/") || u.startsWith("b/") ? u.slice(2) : u;
};

export function splitPatch(patch: string): PatchFile[] {
  if (!patch) return [];
  const lines = patch.split("\n");
  const out: PatchFile[] = [];
  let cur: { lines: string[]; path: string; oldPath?: string; add: number; del: number; binary: boolean; inHunk: boolean } | null =
    null;
  const flush = () => {
    if (!cur) return;
    // Keep the section's own trailing newline shape: every line but the last came with "\n".
    out.push({
      path: cur.path,
      ...(cur.oldPath && cur.oldPath !== cur.path ? { oldPath: cur.oldPath } : {}),
      patch: cur.lines.join("\n") + "\n",
      additions: cur.add,
      deletions: cur.del,
      binary: cur.binary,
    });
    cur = null;
  };
  for (const ln of lines) {
    if (ln.startsWith("diff --git ")) {
      flush();
      // `diff --git a/x b/y` — the fallback path when there are no ---/+++ lines (binary,
      // mode-only). Split on " b/" from the right; quoted paths are handled by unquote.
      const rest = ln.slice("diff --git ".length);
      const at = rest.lastIndexOf(" b/");
      const bq = rest.lastIndexOf(' "b/');
      const cut = bq > at ? bq : at;
      const oldP = cut > 0 ? stripPrefix(rest.slice(0, cut)) : "";
      const newP = cut > 0 ? stripPrefix(rest.slice(cut + 1)) : stripPrefix(rest);
      cur = { lines: [ln], path: newP || oldP, oldPath: oldP || undefined, add: 0, del: 0, binary: false, inHunk: false };
      continue;
    }
    if (!cur) continue;
    cur.lines.push(ln);
    if (!cur.inHunk) {
      if (ln.startsWith("--- ")) {
        const p = ln.slice(4);
        if (p.trim() !== "/dev/null") cur.oldPath = stripPrefix(p);
      } else if (ln.startsWith("+++ ")) {
        const p = ln.slice(4);
        cur.path = p.trim() === "/dev/null" ? (cur.oldPath ?? cur.path) : stripPrefix(p);
      } else if (ln.startsWith("rename from ")) {
        cur.oldPath = ln.slice("rename from ".length);
      } else if (ln.startsWith("rename to ")) {
        cur.path = ln.slice("rename to ".length);
      } else if (ln.startsWith("Binary files ") || ln === "GIT binary patch") {
        cur.binary = true;
      } else if (ln.startsWith("@@")) {
        cur.inHunk = true;
      }
      continue;
    }
    if (ln.startsWith("+")) cur.add++;
    else if (ln.startsWith("-")) cur.del++;
  }
  flush();
  // A trailing "" from the patch's final newline would otherwise add a blank line per file.
  for (const f of out) f.patch = f.patch.replace(/\n+$/, "\n");
  return out;
}
