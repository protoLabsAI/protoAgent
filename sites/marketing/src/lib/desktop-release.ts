// Resolves the newest GitHub release that actually shipped desktop installers.
//
// Source/server releases (PyPI, Docker) and desktop-complete releases are separate
// channels on the same tag sequence: a tag like v0.131.1 can carry only source
// archives while the newest *desktop* build is v0.131.0. The download page must
// therefore never derive installer URLs from "the newest version" (#2514) — it has
// to find the newest release whose assets include the macOS DMG, and link the
// asset's own browser_download_url so every visible link points at a file that
// exists.

const RELEASES_API =
  "https://api.github.com/repos/protoLabsAI/protoAgent/releases?per_page=30";

// The page links exactly these two installers, so a release only counts as
// desktop-complete when BOTH are present — that's what guarantees every visible
// download URL resolves.
const MAC_DMG = /^protoAgent-.+-aarch64-apple-darwin\.dmg$/;
const WIN_SETUP = /^protoAgent-.+-x86_64-pc-windows-msvc-setup\.exe$/;

interface ReleaseAsset {
  name: string;
  browser_download_url: string;
}

interface Release {
  tag_name: string;
  draft: boolean;
  prerelease: boolean;
  published_at: string | null;
  assets: ReleaseAsset[];
}

export interface DesktopRelease {
  tag: string; // "v0.131.0"
  version: string; // "0.131.0"
  date: string; // "2026-08-10" (release published_at)
  dmgUrl: string;
  setupExeUrl: string;
  assets: ReleaseAsset[];
}

export async function fetchLatestDesktopRelease(): Promise<DesktopRelease> {
  const headers: Record<string, string> = {
    Accept: "application/vnd.github+json",
    "User-Agent": "protoagent-marketing-build",
  };
  // Unauthenticated calls from CI runners share a per-IP rate limit pool —
  // marketing-deploy.yml passes the workflow token through.
  const token = process.env.GITHUB_TOKEN;
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(RELEASES_API, { headers });
  if (!res.ok) {
    throw new Error(
      `download page: GitHub releases API returned ${res.status} — refusing to ` +
        `build without a verified desktop release (#2514)`,
    );
  }
  const releases = (await res.json()) as Release[];

  // The API returns newest-first; the first non-draft, non-prerelease release
  // carrying both installers is the newest published desktop build.
  for (const release of releases) {
    if (release.draft || release.prerelease) continue;
    const dmg = release.assets.find((a) => MAC_DMG.test(a.name));
    const setupExe = release.assets.find((a) => WIN_SETUP.test(a.name));
    if (!dmg || !setupExe) continue;
    return {
      tag: release.tag_name,
      version: release.tag_name.replace(/^v/, ""),
      date: (release.published_at ?? "").slice(0, 10),
      dmgUrl: dmg.browser_download_url,
      setupExeUrl: setupExe.browser_download_url,
      assets: release.assets,
    };
  }

  throw new Error(
    `download page: none of the ${releases.length} newest releases has both the ` +
      `macOS DMG and the Windows setup.exe — refusing to publish a download link ` +
      `that would 404 (#2514)`,
  );
}
