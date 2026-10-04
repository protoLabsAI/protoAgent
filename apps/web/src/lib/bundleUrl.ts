// The New-agent "From a bundle URL" source: turn what the operator pasted into the
// `{url, ref}` pair GET /api/archetypes/from-url + POST /api/fleet take — or a readable
// reason it isn't one. The server re-validates with the same rules (operator_api/
// fleet_routes.py `_BUNDLE_URL_RE` + the installer's ref check); this copy only exists so a
// typo is caught before a git fetch.
//
// Accepted: an https git-host repo (`https://github.com/owner/repo`, optional `.git` /
// trailing slash, nested groups for GitLab-style hosts) or the scp-style SSH form
// (`git@github.com:owner/repo.git`). A GitHub page URL pointing INTO a repo at a ref
// (`…/tree/v0.1.0`, `…/releases/tag/v0.1.0`) is folded to the repo + that ref — it's what a
// browser address bar hands you. An explicit ref field wins over one read from the URL.

export type ParsedBundleUrl = { ok: true; url: string; ref?: string } | { ok: false; error: string };

const BUNDLE_URL_RE = /^(?:https:\/\/[A-Za-z0-9.-]+(?::\d+)?\/|git@[A-Za-z0-9.-]+:)[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)+\/?$/;
// The installer's `_REF_RE` (graph/plugins/installer.py) plus its `..` refusal.
const REF_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]*$/;
// `https://github.com/<owner>/<repo>/tree/<ref>` · `…/releases/tag/<ref>` · `…/commit/<sha>`.
const GITHUB_REF_PAGE_RE = /^(https:\/\/github\.com\/[^/]+\/[^/]+?)\/(?:tree|releases\/tag|commit)\/(.+?)\/?$/;

export function isValidBundleRef(ref: string): boolean {
  return REF_RE.test(ref) && !ref.includes("..");
}

export function parseBundleUrl(rawUrl: string, rawRef = ""): ParsedBundleUrl {
  let url = rawUrl.trim();
  let ref = rawRef.trim();
  if (!url) return { ok: false, error: "Paste the bundle's git URL." };
  if (/^http:\/\//i.test(url)) return { ok: false, error: "Use an https:// URL — plain http isn't accepted." };
  const page = GITHUB_REF_PAGE_RE.exec(url);
  if (page) {
    url = page[1];
    if (!ref) ref = decodeURIComponent(page[2]);
  }
  const segments = url.split(/[/:]/);
  if (!BUNDLE_URL_RE.test(url) || segments.some((s) => s === "." || s === "..")) {
    return {
      ok: false,
      error: "That isn't a git repository URL — use https://github.com/<owner>/<repo> (or git@host:owner/repo.git).",
    };
  }
  if (ref && !isValidBundleRef(ref)) {
    return { ok: false, error: "That ref isn't valid — use a tag, branch or commit SHA (e.g. v0.1.0)." };
  }
  return ref ? { ok: true, url, ref } : { ok: true, url };
}
