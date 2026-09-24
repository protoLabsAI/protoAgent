#!/usr/bin/env node
// Guard the desktop `build` so a plain `npm run --workspaces build` (fresh clone, the
// console CI sweep, any contributor machine) does NOT hard-fail on the desktop leg.
//
// `tauri build` bundles the PyInstaller server sidecar as an externalBin
// (src-tauri/binaries/protoagent-server-<triple>, produced by
// apps/desktop/sidecar/build_sidecar.py). Without that binary the bundler aborts with
// "resource path `binaries/protoagent-server-...` doesn't exist" — the only context that
// HAS it is the Desktop Build workflow, which freezes the sidecar first. So:
//
//   * no sidecar present  -> log why and exit 0 (skip the bundle; nothing to build yet)
//   * sidecar present      -> run the real `tauri build`, forwarding every arg verbatim
//                             (the release passes `-- --bundles ... --config '{json}'`)
//
// The release path is unchanged: build_sidecar.py runs before `npm run build`, so the
// binary exists and this delegates straight to `tauri build`.
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const binariesDir = join(here, '..', 'src-tauri', 'binaries');

// The freeze names the binary protoagent-server-<triple>[.exe]; match by prefix so a
// build for any platform target counts as "sidecar present".
const hasSidecar =
  existsSync(binariesDir) &&
  readdirSync(binariesDir).some((name) => name.startsWith('protoagent-server'));

if (!hasSidecar) {
  console.log(
    '[desktop] Skipping `tauri build`: no server sidecar in src-tauri/binaries/. ' +
      'Build it with `python apps/desktop/sidecar/build_sidecar.py` (or use the Desktop ' +
      'Build workflow) first. This keeps `npm run --workspaces build` green without the ' +
      'frozen sidecar.'
  );
  process.exit(0);
}

const args = ['build', ...process.argv.slice(2)];

// Prefer running the CLI's own Node entry with THIS node binary: args pass as an array
// (shell:false), so the release's `--config '{json}'` reaches tauri byte-for-byte on
// every platform, sidestepping cmd/sh quoting. Locate the package by walking up the
// node_modules chain (hoisted to the repo root, or under apps/desktop) and reading its
// package.json off disk — no `exports`-map resolution to trip over.
function findTauriBin() {
  let dir = here;
  for (let i = 0; i < 8; i += 1) {
    const pkgJson = join(dir, 'node_modules', '@tauri-apps', 'cli', 'package.json');
    if (existsSync(pkgJson)) {
      const bin = JSON.parse(readFileSync(pkgJson, 'utf8')).bin;
      const rel = typeof bin === 'string' ? bin : bin && bin.tauri;
      const abs = rel && join(dirname(pkgJson), rel);
      if (abs && existsSync(abs)) return abs;
      return null;
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

const tauriBin = findTauriBin();
const result = tauriBin
  ? spawnSync(process.execPath, [tauriBin, ...args], { stdio: 'inherit' })
  : // Last resort: the `tauri` shim npm puts on PATH (node_modules/.bin); shell on
    // Windows, where the shim is tauri.cmd.
    spawnSync('tauri', args, { stdio: 'inherit', shell: process.platform === 'win32' });

if (result.error) {
  console.error(result.error);
  process.exit(1);
}
process.exit(result.status ?? 1);
