#!/usr/bin/env python3
"""Freeze the headless protoAgent server into the Tauri sidecar.

On Linux and Windows this produces the single-file
``apps/desktop/src-tauri/binaries/protoagent-server-<target-triple>`` — the
``externalBin`` Tauri bundles and launches. On macOS it produces a signed onedir tree
at ``binaries/protoagent-server-onedir/`` instead, which ``tauri.macos.conf.json``
bundles into ``Contents/Resources`` (see ``freeze_mode`` for why). The Tauri webview renders its
own copy of the React console; the binary ALSO bundles the console build
(``apps/web/dist``) so `/app` serves it — the `--ui console` contract, and what
a QR-paired phone loads (#2411).

Run from a venv with the runtime deps + PyInstaller installed:

    pip install -r requirements.txt pyinstaller "mcp[cli]"
    python apps/desktop/sidecar/build_sidecar.py

(``mcp[cli]`` is a build-env-only need: ``--collect-all mcp`` imports every mcp
submodule, and ``mcp.cli`` raises at import when typer is missing.)

The target triple matches Tauri's expectation (``rustc`` host), so the binary
lands at the exact name Tauri looks for during ``tauri build``.

The desktop no longer bundles the beads-rust ``br`` CLI (#3271, was #3236): the
project_board plugin's own resolution (explicit ``BR_BIN`` > its fetched copy >
``PATH``) is the single story again, on every platform.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BINARIES_DIR = REPO / "apps" / "desktop" / "src-tauri" / "binaries"
NAME = "protoagent-server"
#: macOS ships a ONEDIR tree instead of the onefile binary (see ``freeze_mode``):
#: ``binaries/protoagent-server-onedir/`` → ``Contents/Resources/protoagent-server/``
#: (``src-tauri/tauri.macos.conf.json``), launched by ``sidecar.rs::server_command``.
ONEDIR_DEST = BINARIES_DIR / f"{NAME}-onedir"
ENTITLEMENTS = REPO / "apps" / "desktop" / "src-tauri" / "entitlements.plist"
#: Developer ID identity for the onedir tree's Mach-O files (CI sets it from
#: APPLE_SIGNING_IDENTITY when it signs a release). Unset → ad-hoc signing (a local or
#: unsigned test build). Tauri signs only the binaries it bundles itself; it never
#: re-signs a resource tree, so the tree arrives in the app already signed.
CODESIGN_IDENTITY_ENV = "PROTOAGENT_CODESIGN_IDENTITY"

# Read-only defaults the frozen server reads via REPO_ROOT/config (which
# resolves to _MEIPASS/config inside the bundle). Live state (langgraph-config
# .yaml, secrets.yaml, .setup-complete) is NOT bundled — it's written at
# runtime to PROTOAGENT_CONFIG_DIR. Never bundle secrets.yaml or the live YAML.
BUNDLED_DATA: list[tuple[str, str]] = [
    # Version truth (version-coherence Cross-cutting B, docs/dev/version-coherence.md):
    # `paths.package_version()` has no installed-package metadata in a frozen binary,
    # so it falls back to reading `pyproject.toml` at _MEIPASS. Bundle it (top level)
    # so the desktop app reports its REAL version instead of "0.0.0" — which otherwise
    # blinds the A2A card, the fleet version handshake, runtime status, and the plugin
    # `min_protoagent_version` compat gate (every plugin with one is wrongly refused).
    ("pyproject.toml", "."),
    ("config/langgraph-config.example.yaml", "config"),
    ("config/SOUL.md", "config"),
    # The curated Discover directory (ADR 0059). The catalog route reads it from
    # REPO_ROOT/config (→ _MEIPASS/config when frozen); without it the desktop app's
    # Plugins ▸ Discover shows "0 official plugins" (every entry is unreachable).
    ("config/plugin-catalog.json", "config"),
    # The curated common-MCP-server picker (Settings ▸ MCP ▸ Browse common servers).
    # `/api/mcp/catalog` reads it from REPO_ROOT/config (→ _MEIPASS/config when frozen);
    # without it the desktop app shows "no servers match" (same trap as the plugin
    # catalog above).
    ("config/mcp-catalog.json", "config"),
    # The new-agent archetype directory (ADR 0042/0083). `_load_archetype_catalog`
    # (operator_api/fleet_routes.py) reads it from REPO_ROOT/config (→ _MEIPASS/config
    # when frozen) and falls back to Basic + Custom when it's absent — so without this
    # entry the desktop app's new-agent picker silently loses every catalog archetype
    # (Cowork), and nothing errors. Same trap as the plugin/MCP catalogs above.
    ("config/archetype-catalog.json", "config"),
    ("config/soul-presets", "config/soul-presets"),
    # The document-baseline pin list (ADR 0092 / ADR 0094). runtime/python_install.py
    # reads it (→ _MEIPASS/sidecar/requirements-docs.txt when frozen) to pip the doc
    # libs into the managed CPython the execute_code child spawns — the copies
    # DOC_COLLECT_ALL bundles into the PYZ are host-process-only and unreachable from
    # any external interpreter, so without this entry the provisioned runtime would
    # have an interpreter but no python-docx/openpyxl/pptx/reportlab to import.
    ("apps/desktop/sidecar/requirements-docs.txt", "sidecar"),
    # The bundled first-party skills (config/skills/*/SKILL.md — release-notes,
    # web-research). `_build_skills_index` (server/agent_init.py) seeds the skill
    # index from REPO_ROOT/config/skills (→ _MEIPASS/config/skills when frozen),
    # exactly like SOUL.md/soul-presets above. Without this entry the directory
    # never lands in the bundle, so the frozen desktop app indexes 0 SKILL.md
    # skills — the `/skill` library is silently empty.
    ("config/skills", "config/skills"),
    ("static", "static"),
    # The full console build (apps/web/dist, base /app/) — served by the frozen
    # sidecar at /app so `--ui console` is truthful AND mobile QR pairing works: a
    # paired phone loads the SPA from the SIDECAR, not the Tauri webview (#2411).
    # This also carries dist/_ds (the DS plugin-kit every plugin iframe loads).
    # ORDER CONTRACT (enforced by the preflight below): the workflow must run
    # `npm run build --workspace @protoagent/web` BEFORE this freeze; tauri's
    # beforeBuildCommand later REBUILDS dist with `--base /` for its own webview —
    # harmless, because our copy is already inside the binary by then.
    ("apps/web/dist", "apps/web/dist"),
    # Bundled first-party plugins (ADR 0018/0019). Loaded by file path (importlib)
    # at runtime, so PyInstaller's import-scan misses them; ship the tree as data so
    # the frozen app finds them under _MEIPASS/plugins (the loader's bundle root).
    # Externals (Discord, Google, …) install at runtime into the live dir (ADR 0058).
    ("plugins", "plugins"),
    # The scaffolder vendors testkit.py into with_tests suites by reading it beside
    # its own __file__ — which lives in the PYZ with no source on disk when frozen.
    # Ship the one file as data so scaffold_plugin(with_tests=True) works on desktop.
    ("src/graph/plugins/testkit.py", "graph/plugins"),
    # The published docs corpus — the `docs` plugin reads it (`docs/` beside the bundled
    # `plugins/` under _MEIPASS) to power docs_search/docs_read + the Docs view. Only the
    # Diátaxis sections + ADRs; `docs/dev` (internal) and `.vitepress` (build) are excluded.
    ("docs/tutorials", "docs/tutorials"),
    ("docs/guides", "docs/guides"),
    ("docs/reference", "docs/reference"),
    ("docs/explanation", "docs/explanation"),
    ("docs/adr", "docs/adr"),
    ("docs/public/media", "docs/public/media"),
]

# Packages PyInstaller's static analysis under-collects (dynamic imports +
# importlib.metadata entry points). --collect-all pulls modules + data + metadata.
COLLECT_ALL = [
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langgraph",
    "ddgs",
    "langfuse",
    "croniter",
    # stdlib zoneinfo's fallback IANA database — Windows has no system tz data,
    # and zoneinfo imports tzdata DYNAMICALLY, so the import-scan misses it;
    # without the collect every ZoneInfo(...) in the frozen sidecar raised (#1683).
    "tzdata",
    # A2A 1.0 (ADR 0014): the SDK + the protoLabs conventions layer (git-dep).
    # Both pull submodules/metadata that a bare import-scan misses — without a
    # full collect, the frozen `protolabs_a2a` is missing `build_agent_card`.
    "a2a",
    "protolabs_a2a",
    # a2a-sdk[sqlite] durable task/push stores: imported lazily by the SDK, so
    # the import-scan misses them — collect explicitly.
    "aiosqlite",
    "sqlalchemy",
    # Comms-plugin runtime deps (ADR 0058): a plugin installed at runtime in the
    # frozen app can only import what's bundled. `websockets` is what the Discord /
    # Slack comms plugins use (httpx is already pulled by the core deps); `tools` is
    # the core tools package, collected whole because a plugin may import a core tool
    # module by file path (the import-scan misses those).
    "websockets",
    "tools",
    # MCP client (tools/mcp_tools) — connect external MCP servers; the SDK is lazily
    # imported, so collect it explicitly. (An external Google Gmail/Calendar MCP
    # plugin re-invokes the frozen binary via the generic ``--mcp-plugin <id>`` shim.)
    "mcp",
    # Markdown renderer for the bundled `docs` plugin's reader view — imported only by
    # the path-loaded plugin, so the import-scan misses it; collect explicitly.
    "markdown_it",
    # The pip-less wheel installer (ADR 0093) resolves deps with packaging.markers /
    # .specifiers / .requirements / .version at runtime in the frozen app. They're
    # imported lazily inside wheel_installer, so collect the whole package (+ its
    # vendored parser data) to be safe rather than rely on the import-scan.
    "packaging",
    # The fleet deck (#3473): `protoagent-server fleet` opens the Textual TUI from the desktop
    # binary, and a desktop-only install has no other `protoagent` on the box. Textual resolves
    # its widgets through a lazy `textual.widgets.__getattr__` (a static scan sees none of the
    # widget modules the deck names) and its dist-info METADATA is read at import; `deck.app`
    # and `deck.hubs` — the Textual screens, and everything they pull in — are reached only by
    # name from `graph.fleet.cli` (importlib, so `--help` and the member verbs never load
    # Textual), while `deck.hub` / `deck.data` / `deck.discovery` are its static, Textual-free
    # imports. Collect both packages whole. (~2.4 MB compressed on a 74 MB binary.) Every
    # desktop-build leg then RUNS the frozen deck (`scripts/fleet_deck_smoke.py --bin`, #3498):
    # the hidden `fleet --self-check` opens it headlessly and paints its first screens, so a
    # module either package reaches lazily that this collect misses fails the leg.
    "textual",
    "deck",
]

# Google client libraries (ADR 0017) — bundled only when installed in the build
# env (``requirements-google.txt``). Keeps a lean build (no google) working;
# install the extra to ship Gmail/Calendar in the frozen app.
OPTIONAL_COLLECT_ALL = [
    "google.auth",
    "google.oauth2",
    "google_auth_oauthlib",
    "googleapiclient",
]

# Document-generation stack (ADR 0092) — the knowledge-work baseline the desktop
# app ships ON by default so first-party skill packs (cowork's docx/xlsx/pptx/pdf
# skills) and plugins like protobanana (Pillow) actually produce files on the
# frozen runtime instead of hitting the ADR 0058 D2 "install on a server/Docker"
# refusal. Installed in CI from ``requirements-docs.txt``; a lean local freeze
# without it still works (find_spec-guarded, like the Google tier). ``--collect-all``
# is REQUIRED here — reportlab ships font/AFM data, python-docx/pptx ship their
# default .docx/.pptx templates as package data, and a bare import-scan misses all
# of it (the library raises at runtime opening its default template).
#
# Each entry is (import_name, distribution_name) — they DIFFER for the ones that matter
# (python-docx→``docx``, python-pptx→``pptx``, Pillow→``PIL``), and that gap is load-bearing:
# the ADR 0058 D2 gate checks a plugin's ``requires_pip`` with installer._importable, which
# tries ``importlib.metadata.version(<dist>)`` FIRST. So we ``--copy-metadata <dist>`` alongside
# ``--collect-all <import>`` — otherwise cowork's ``requires_pip: [python-docx, …]`` could still
# be refused on the frozen app even though ``import docx`` works, because the dist-info wasn't
# bundled. (lxml + PIL also arrive transitively, but naming them keeps a runtime-installed
# plugin's lazy ``import PIL`` — protobanana — importable when nothing else pulled it.)
DOC_COLLECT_ALL = [
    ("docx", "python-docx"),
    ("openpyxl", "openpyxl"),
    ("pptx", "python-pptx"),
    ("reportlab", "reportlab"),
    ("lxml", "lxml"),
    ("PIL", "pillow"),
]

# tkinter is GUI dead weight in a headless server and a freeze hazard — exclude it.
# (Gradio + the chat_ui module it backed were removed from the project entirely.)
EXCLUDE = ["tkinter"]

# CLI subcommand modules that `server/cli.py::dispatch` reaches ONLY via a dynamic
# `importlib.import_module(...)` string (the `_FORWARD` table) — e.g. Fleet ▸ Add Agent
# spawns `<frozen> plugin install <bundle>` → `import_module("graph.plugins.cli")`.
# PyInstaller's static import scan can't follow a string import, so without an explicit
# hidden-import each of these is ABSENT from the bundle and the verb dies with
# `ModuleNotFoundError` in the frozen app (#2136 — cowork create; also fleet/skills/
# runtime/operator-mcp, i.e. ACP). MUST stay in sync with `server.cli._FORWARD` — the
# test `test_sidecar_bundles_every_forwarded_cli_module` fails if a new verb is added
# without collecting it here. --hidden-import pulls the module + its static import chain.
# NOTE (fleet deck, #3468 → #3473): `graph.fleet.cli` imports `deck.app` (the Textual deck, which
# pulls `deck.hubs` and the other screens) and `deck.discovery` BY NAME (importlib), so
# `protoagent --help` and the non-interactive verbs never load Textual (`deck.hub` / `deck.data`
# are its static imports; `deck.discovery` is Textual-free by contract);
# the deck and Textual are bundled through COLLECT_ALL above (S6's decision), not listed here —
# `test_sidecar_bundles_the_fleet_deck` pins that. A build that drops them makes bare
# `protoagent fleet` / `top` print the one-line "not available in this build" hint (exit 2).
CLI_FORWARD_MODULES = [
    "graph.plugins.cli",
    "graph.workspaces.cli",
    "graph.skills.cli",
    "graph.fleet.cli",
    "graph.usage_cli",
    "graph.config_explain",
    "graph.model_cli",
    "runtime.cli",
    "ops.cli",
    "ops.reset",
    "server.knowledge_cli",
    "server.operator_mcp",
    "graph.snapshot_cli",
    "deck.pair",
    "graph.checkpoint_migrate",
    "infra.cli_link",
]


def freeze_mode(platform: str = sys.platform) -> str:
    """``onedir`` on macOS, ``onefile`` elsewhere.

    A onefile unpacks itself (~5,000 files, ~200 MB) into a fresh ``$TMPDIR/_MEI*`` dir
    on every launch, and macOS scans each freshly written Mach-O the first time it is
    loaded. Measured on v0.231.0 (the stress test of 2026-11-07): ``protoagent-server
    --help`` took 14-16 s wall for 1.3 s of CPU; ``sample`` put 2,360 of 2,567 main-thread
    samples in dyld's code-signature registration (``fcntl``), and dlopen()ing a copy of
    the extracted libraries took 47 s the first time and 0.18 s the second. The desktop's
    own server paid the same on every app start. A onedir tree lives at one stable path
    inside the signed app, so that cost is paid once, not on every run.

    Linux and Windows keep the onefile: no per-file first-load scan there (Linux), and
    the Windows installer's ``_MEI`` sweep and AV notes assume it (follow-up if Defender
    proves to cost the same)."""
    return "onedir" if platform == "darwin" else "onefile"


_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
}


def is_macho(path: Path) -> bool:
    """True for a thin or universal Mach-O file (by magic, not by name: Python
    extensions are ``.so``, the bootloader and ``Python`` have no suffix)."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    if head[:4] in _MACHO_MAGICS:
        return True
    # FAT_MAGIC is shared with Java .class files; a fat header's arch count is tiny,
    # a class file's version field is not.
    return head[:4] == b"\xca\xfe\xba\xbe" and len(head) == 8 and int.from_bytes(head[4:], "big") < 32


def _walk(root: Path):
    """Every entry under ``root``, never descending through a directory symlink."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            yield Path(dirpath) / name


def dissolve_frameworks(root: Path) -> list[str]:
    """Replace every ``<Name>.framework`` bundle under ``root`` with its bare binary.

    A python.org (framework) build makes PyInstaller collect ``_internal/Python.framework``
    with the versioned layout (``Versions/Current`` and the top-level ``Python`` and
    ``Resources`` symlinks) plus ``_internal/Python``, a symlink to the binary that the
    bootloader loads. That bundle can't survive the trip to ``Contents/Resources``:
    Tauri skips directory symlinks, and once they are materialized the framework root
    holds a real ``Python``, which codesign refuses to sign ("bundle format is
    ambiguous (could be app or framework)", desktop-build run 42455809696). A framework
    can't be signed as a bundle and then copied flat either: the copy breaks its seal.

    Nothing needs the bundle: dyld and the bootloader load a Mach-O by path. So the
    one ``Versions/<v>/<Name>`` binary becomes a real file at ``<parent>/<Name>`` (where
    PyInstaller's own ``_internal/Python`` link already points), and :func:`relink_dissolved`
    rewrites any load command that named it through the framework. A framework carrying
    anything beyond its binary and ``Info.plist`` (several versions, other resources,
    helpers) is refused, never dropped silently. Returns the dissolved names."""
    names: list[str] = []
    fws = sorted(
        (p for p in _walk(root) if p.suffix == ".framework" and p.is_dir() and not p.is_symlink()),
        key=lambda p: -len(p.parts),
    )
    for fw in fws:
        name = fw.stem
        vdir = fw / "Versions"
        versions = sorted(v for v in vdir.iterdir() if v.is_dir() and not v.is_symlink()) if vdir.is_dir() else []
        if len(versions) != 1:
            raise SystemExit(f"{fw}: expected exactly one Versions/<v> directory, found {[v.name for v in versions]}")
        binary = versions[0] / name
        if not is_macho(binary):
            raise SystemExit(f"{fw}: {binary} is not a Mach-O file")
        extra = sorted(
            str(p.relative_to(versions[0]))
            for p in _walk(versions[0])
            if (p.is_file() or p.is_symlink())
            and p != binary
            and p.name != "Info.plist"
            and p.relative_to(versions[0]).parts[0] != "_CodeSignature"
        )
        if extra:
            raise SystemExit(f"{fw}: refusing to dissolve a framework that ships more than its binary: {extra}")
        dest = fw.parent / name
        if dest.is_symlink() or dest.exists():
            if not dest.is_symlink() or dest.resolve() != binary.resolve():
                raise SystemExit(f"{dest} already exists and is not a link to {binary}")
            dest.unlink()
        shutil.copy2(binary, dest)
        shutil.rmtree(fw)
        names.append(name)
    return names


def dissolved_ref(ref: str, names: list[str]) -> str | None:
    """The new install name for a load command that pointed into a dissolved framework,
    or None to leave it alone. ``@rpath/Python.framework/Versions/3.12/Python`` becomes
    ``@rpath/Python``: same directory, since :func:`dissolve_frameworks` put the binary
    beside where the framework was. Absolute paths name system frameworks; untouched."""
    if not ref.startswith("@"):
        return None
    for name in names:
        marker = f"/{name}.framework/"
        if ref.endswith(f"/{name}") and marker in ref:
            return f"{ref.split(marker, 1)[0]}/{name}"
    return None


def relink_dissolved(root: Path, names: list[str]) -> list[str]:
    """Rewrite every Mach-O load command (and dylib id) under ``root`` that names a
    dissolved framework. Runs before signing, since ``install_name_tool`` voids a
    signature. Returns ``"<file>: <old> -> <new>"`` lines for the build log."""
    changed: list[str] = []
    if not names:
        return changed
    for path in (p for p in _walk(root) if p.is_file() and not p.is_symlink() and is_macho(p)):
        own = {
            line.strip()
            for line in _run_or_explain(["otool", "-D", str(path)]).stdout.splitlines()[1:]
            if line.strip() and not line.startswith("(")
        }
        out = _run_or_explain(["otool", "-L", str(path)]).stdout
        refs = {line.strip().split(" (", 1)[0] for line in out.splitlines() if line.startswith("\t")} - own
        for ref in sorted(refs):
            new = dissolved_ref(ref, names)
            if new and new != ref:
                _run_or_explain(["install_name_tool", "-change", ref, new, str(path)])
                changed.append(f"{path.relative_to(root)}: {ref} -> {new}")
        for ref in sorted(own):
            new = dissolved_ref(ref, names)
            if new and new != ref:
                _run_or_explain(["install_name_tool", "-id", new, str(path)])
                changed.append(f"{path.relative_to(root)}: id {ref} -> {new}")
    return changed


def _run_or_explain(call: list[str]) -> subprocess.CompletedProcess:
    """Run a build tool; on failure print its full stderr/stdout (the CI log is all we
    get) and raise."""
    proc = subprocess.run(call, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"FAILED ({proc.returncode}): {' '.join(call)}", file=sys.stderr)
        for stream in (proc.stdout, proc.stderr):
            if stream.strip():
                print(stream.rstrip(), file=sys.stderr)
        raise subprocess.CalledProcessError(proc.returncode, call, proc.stdout, proc.stderr)
    return proc


def materialize_symlinks(root: Path) -> int:
    """Replace every symlink under ``root`` with a real copy of what it points at.

    Tauri copies a resource directory file by file: it follows a file symlink (a
    duplicate, which is harmless) and SKIPS a directory symlink outright, so a linked
    directory would silently vanish from the app. Materializing first makes the tree
    Tauri copies identical to the tree that was signed. Frameworks are dissolved before
    this runs (:func:`dissolve_frameworks`). Returns how many it replaced."""
    links = sorted((p for p in _walk(root) if p.is_symlink()), key=lambda p: len(p.parts))
    for link in links:
        if not link.is_symlink():
            continue  # already replaced as part of a parent directory copy
        target = link.resolve()
        link.unlink()
        if target.is_dir():
            shutil.copytree(target, link, symlinks=False)
        elif target.exists():
            shutil.copy2(target, link)
    return len(links)


def codesign_plan(root: Path, exe: Path, identity: str, entitlements: Path) -> list[list[str]]:
    """The ``codesign`` calls that sign a onedir tree, inside out.

    Every Mach-O file is signed on its own, deepest first, and the bootloader last with
    the app's entitlements (it is the process: ``disable-library-validation`` must reach
    it, as Tauri gives it to the externalBin elsewhere). A real identity adds the hardened
    runtime and a secure timestamp, which notarization requires of every Mach-O in the
    app; ad-hoc (``-``) signing gets neither."""
    base = ["codesign", "--force", "--sign", identity]
    if identity != "-":
        base += ["--options", "runtime", "--timestamp"]
    files = [p for p in _walk(root) if p.is_file() and not p.is_symlink() and p != exe and is_macho(p)]
    files.sort(key=lambda p: (-len(p.parts), str(p)))
    plan = [[*base, str(p)] for p in files]
    plan.append([*base, "--entitlements", str(entitlements), str(exe)])
    return plan


def finish_onedir(built: Path, dest: Path, identity: str) -> None:
    """Dissolve frameworks, materialize, sign and install a onedir build at ``dest``."""
    exe = built / NAME
    names = dissolve_frameworks(built)
    relinked = relink_dissolved(built, names)
    print(f"onedir: dissolved frameworks {names or 'none'}; {len(relinked)} load commands rewritten")
    for line in relinked:
        print(f"  {line}")
    n = materialize_symlinks(built)
    plan = codesign_plan(built, exe, identity, ENTITLEMENTS)
    print(
        f"onedir: {n} symlinks materialized; signing {len(plan)} Mach-O files ({'ad-hoc' if identity == '-' else identity})"
    )
    for call in plan:
        _run_or_explain(call)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(built, dest, symlinks=True)


def target_triple() -> str:
    """Tauri names sidecars ``<bin>-<rustc-host-triple>``; match it."""
    out = subprocess.run(["rustc", "-Vv"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("could not determine rustc host triple (is rustc installed?)")


def main() -> None:
    triple = target_triple()
    mode = freeze_mode()
    work = REPO / "build" / "sidecar"
    sep = ";" if os.name == "nt" else ":"

    # Preflight the console build (#2411). The BUNDLED_DATA loop below warns-and-
    # skips on missing paths — right for optional tiers, but the console is a
    # CONTRACT: the sidecar launches with `--ui console`, and mobile QR pairing
    # serves the SPA from this binary. Freeze without it and every desktop
    # install ships a 404 /app. Fail the build loudly instead.
    dist = REPO / "apps" / "web" / "dist"
    if not (dist / "index.html").exists() or not (dist / "_ds").is_dir():
        sys.exit(
            "build_sidecar: apps/web/dist is missing or incomplete (need index.html + _ds/).\n"
            "Run the console build FIRST — the freeze bundles it:\n"
            "    npm ci && node apps/web/scripts/copy-plugin-kit.mjs\n"
            "    npm run build --workspace @protoagent/web\n"
            "(tauri's beforeBuildCommand rebuilds dist with --base / afterwards; that's fine —\n"
            "this freeze's copy is already inside the binary by then. See #2411.)"
        )

    add_data: list[str] = []
    for src, dest in BUNDLED_DATA:
        if (REPO / src).exists():
            add_data += ["--add-data", f"{REPO / src}{sep}{dest}"]
        else:
            print(f"warning: bundled data missing, skipping: {src}", file=sys.stderr)

    collect: list[str] = []
    for pkg in COLLECT_ALL:
        collect += ["--collect-all", pkg]
    # Optional tiers: collect only what's importable in this build env, so a lean
    # freeze (extra not installed) still succeeds instead of erroring on a --collect
    # of a missing package.
    import importlib.util

    def _collect_if_present(pkgs: list[str], hint: str) -> None:
        for pkg in pkgs:
            if importlib.util.find_spec(pkg) is not None:
                collect.extend(["--collect-all", pkg])
            else:
                print(f"note: optional package not installed, skipping: {pkg} ({hint})", file=sys.stderr)

    _collect_if_present(OPTIONAL_COLLECT_ALL, "install requirements-google.txt to ship Gmail/Calendar")
    # Doc stack: --collect-all the module AND --copy-metadata the distribution, so the ADR 0058
    # D2 gate's metadata-first check (installer._importable → metadata.version(<dist>)) resolves
    # in the frozen app despite the dist≠import name gap. Guarded on the import name being present
    # (a lean local freeze without requirements-docs.txt just skips the tier).
    for imp, dist in DOC_COLLECT_ALL:
        if importlib.util.find_spec(imp) is not None:
            collect.extend(["--collect-all", imp, "--copy-metadata", dist])
        else:
            print(
                f"note: optional package not installed, skipping: {imp} "
                "(install requirements-docs.txt to ship the document-generation stack)",
                file=sys.stderr,
            )
    exclude: list[str] = []
    for mod in EXCLUDE:
        exclude += ["--exclude-module", mod]

    # Collect the dynamically-dispatched CLI modules (#2136) — the static scan misses them.
    hidden: list[str] = []
    for mod in CLI_FORWARD_MODULES:
        hidden += ["--hidden-import", mod]

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        f"--{mode}",
        "--noconfirm",
        "--clean",
        "--name",
        NAME,
        "--distpath",
        str(work / "dist"),
        "--workpath",
        str(work / "build"),
        "--specpath",
        str(work),
        "--paths",
        str(REPO / "src"),
        "--paths",
        str(REPO),
        *exclude,
        *hidden,
        *add_data,
        *collect,
        # ADR 0023: server.py is now the `server` package; freeze its module
        # entry point. PyInstaller bundles the whole `server` package and the
        # `--add-data` assets land at _MEIPASS top level (server/_bundle_root
        # resolves there when frozen).
        str(REPO / "src" / "server" / "__main__.py"),
    ]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(REPO))

    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    if mode == "onedir":
        finish_onedir(work / "dist" / NAME, ONEDIR_DEST, os.environ.get(CODESIGN_IDENTITY_ENV) or "-")
        print(f"\nsidecar (onedir) -> {ONEDIR_DEST}/{NAME}")
        return
    suffix = ".exe" if os.name == "nt" else ""
    built = work / "dist" / f"{NAME}{suffix}"
    dest = BINARIES_DIR / f"{NAME}-{triple}{suffix}"
    shutil.copy2(built, dest)
    os.chmod(dest, 0o755)
    print(f"\nsidecar -> {dest}")


if __name__ == "__main__":
    main()
