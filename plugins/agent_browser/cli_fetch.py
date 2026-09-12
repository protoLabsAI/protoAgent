"""The `agent-browser` CLI, fetched on first use — pinned and checksum-verified.

Until this, the plugin was PATH-only: an operator without the CLI got a setup-gap banner
telling them to ``npm i -g agent-browser``, which needs Node on the host for what is,
underneath the npm launcher, a single native Rust binary. The operator's ruling
(2026-09-12): download that binary on first use. This is a DOWNLOAD of a pinned upstream
release asset at runtime, not a binary shipped inside protoAgent, so the no-bundled-``br``
order doesn't apply.

Resolution order for the CLI the plugin shells (``preflight.locate``)::

    the operator's ``binary`` setting, when it is a path or a non-default command name
      >  ``agent-browser`` on PATH              (an operator-installed CLI always wins)
      >  the binary this module fetched          (version-keyed, sha256-verified)

**When it fetches.** The first browser command that finds no CLI (``tools`` / the panel's
Start, when ``cli_autofetch`` is on — the default), or the operator's click on the banner's
"Download agent-browser" button (a ``plugin_setup`` action → the ``download-cli`` step),
which works whatever the knob says because the click IS the operator's explicit choice.
Never at boot: enabling the plugin isn't using it. Chrome is a different matter — its
~150 MB install runs only from its own button (``chrome_install``), never from a tool call.

**The pin.** ``CLI_VERSION`` is the release the plugin is verified against: its argv option
guard (``runtime.bad_operand``) and its ``doctor --json`` reading were checked on 0.27.1.
Move it DELIBERATELY, with the sha256s — never to "latest".

**The checksums are pinned HERE because upstream publishes none.** A vercel-labs/agent-browser
release is the seven raw executables and nothing else: no ``SHA256SUMS``, no per-asset
``.sha256``, no signature. Each sha256 below was computed by downloading the asset
(2026-09-12) and cross-checked against the digest GitHub's release API reports for that asset
(``gh release view v0.27.1 -R vercel-labs/agent-browser --json assets``) — two independent
reads that agree. The assets are raw binaries, not archives, so there's no extraction step:
the verified bytes ARE the installed file.

**Where it lands.** ``instance_paths().cache_dir / "agent-browser" / <version> /
agent-browser[.exe]`` — the BOX tier (ADR 0065), not one instance's root. The content is
immutable (pinned + verified), so the dev sandbox and every fleet member on this machine
share one download, the way they already share the Chrome the CLI installs
(``~/.agent-browser/browsers``). Keyed by version, so a pin bump fetches the new release
instead of running the old one. ``AGENT_BROWSER_CLI_DIR`` overrides the base (tests, or an
air-gapped host that pre-seeds the file — which still has to match the pin to be used).

**Install is atomic.** The bytes are verified BEFORE anything is written; then a temp file
beside the target, ``chmod 0755`` (POSIX only — Windows has no execute bit), and one
``os.replace``. A corrupted download never touches the target, and no half-written binary
ever sits at the resolved path. On Windows, ``os.replace`` over a binary another process is
RUNNING raises ``PermissionError``: that's retried a few times with backoff, then accepted
if the file already there is the pinned one (another instance won the race with the same
bytes), and refused otherwise. The resolved file is re-verified once per (size, mtime), so
a binary swapped behind our back is ignored rather than run.

**Egress** mirrors ``br_fetch`` (project_board): the host's allowlist (``security.egress``,
ADR 0008) is consulted for the URL and for every redirect hop, and a hop must stay on HTTPS
``*.githubusercontent.com`` (GitHub serves release assets from
``release-assets.githubusercontent.com``).

The fetch state lives in a process-stable ``sys.modules`` slot, so a plugin reload neither
re-fetches nor loses a download in flight.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import platform as _platform
import sys
import tempfile
import threading
import time
import types
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("protoagent.plugins.agent_browser")

# ── the pin ──────────────────────────────────────────────────────────────────────
CLI_VERSION = "0.27.1"
RELEASE_URL = "https://github.com/vercel-labs/agent-browser/releases/download/v{version}/{asset}"
# platform key → (release asset, sha256 of that asset). Keys are upstream's own
# `<os>-<arch>` naming, exactly as its npm postinstall computes it (`linux-musl` on musl).
ASSETS: dict[str, tuple[str, str]] = {
    "darwin-arm64": ("agent-browser-darwin-arm64", "5ff18a2f0c7d4c662d638b8ac5ce434b589be5f6bde8b8fe7e25c3658e2bcbf9"),
    "darwin-x64": ("agent-browser-darwin-x64", "de70b31d7dc86f3ad2f9df016b5109306079569de61c8825f836c12a34d4f1d7"),
    "linux-x64": ("agent-browser-linux-x64", "95ff8224a971698d9df8add26f1f571027c35f9003e3067c53e54d154b5b1ea1"),
    "linux-arm64": ("agent-browser-linux-arm64", "ab93f04ca217e6ff73832c900a43c4b88f14239d6bf11fb8ba90478d99b84b3d"),
    "linux-musl-x64": (
        "agent-browser-linux-musl-x64",
        "7fb2f2cb1d503b4af55b337af2bf379da0c85655aa0ea3685e80c39bd0aaf1fd",
    ),
    "linux-musl-arm64": (
        "agent-browser-linux-musl-arm64",
        "807c1c386e4cc21dc4e1f8ee747a40650a5f560ed21dc25054464bc2f6fb52df",
    ),
    "win32-x64": ("agent-browser-win32-x64.exe", "ac88ef4261ccae30d047506a8c45d465f6c7b7a96743189131e6fb0b841bc3b1"),
}
FETCH_TIMEOUT_S = 120.0
MAX_ASSET_BYTES = 64 * 1024 * 1024  # a release binary is ~10-12 MB; refuse anything absurd
ENV_CLI_DIR = "AGENT_BROWSER_CLI_DIR"
# Where a redirect hop may land: GitHub serves release assets from
# release-assets.githubusercontent.com (the same hop br_fetch verified live).
REDIRECT_HOST_SUFFIX = ".githubusercontent.com"
# A leftover temp must be this old before an install sweeps it: the cache is machine-shared,
# so a younger one may be ANOTHER instance's download in flight.
STALE_TEMP_S = 15 * 60
_TEMP_PREFIX = ".agent-browser-"
# os.replace over a running binary (Windows) — bounded: ~7.75 s of backoff in total.
_REPLACE_ATTEMPTS = 6
_REPLACE_BACKOFF_S = 0.25


class ChecksumError(ValueError):
    """The downloaded bytes are not the pinned release asset."""


@dataclass(frozen=True)
class FetchSpec:
    version: str
    platform: str
    asset: str
    url: str
    sha256: str


# ── platform ──────────────────────────────────────────────────────────────────────
_OS = {"darwin": "darwin", "linux": "linux", "windows": "win32", "win32": "win32"}
_ARCH = {"x86_64": "x64", "amd64": "x64", "x64": "x64", "arm64": "arm64", "aarch64": "arm64"}


def is_musl() -> bool:
    """A musl libc host (Alpine). ``platform.libc_ver()`` names glibc when it can read the
    interpreter; musl shows as an empty name, so also look for musl's loader."""
    try:
        if _platform.libc_ver()[0] == "glibc":
            return False
    except Exception:  # noqa: BLE001 — an unreadable interpreter is not a verdict
        pass
    try:
        return bool(list(Path("/lib").glob("ld-musl-*.so.1")))
    except OSError:
        return False


def platform_key(system: str | None = None, machine: str | None = None, *, musl: bool | None = None) -> str | None:
    """This host's key in ``ASSETS`` (``darwin-arm64``, ``linux-musl-x64``, ``win32-x64``,
    …), or None when upstream publishes no build for it (Windows on ARM, FreeBSD, riscv)."""
    os_name = _OS.get((system or _platform.system()).strip().lower())
    arch = _ARCH.get((machine or _platform.machine()).strip().lower())
    if not os_name or not arch:
        return None
    if os_name == "linux" and (is_musl() if musl is None else musl):
        os_name = "linux-musl"
    key = f"{os_name}-{arch}"
    return key if key in ASSETS else None


def describe_platform(system: str | None = None, machine: str | None = None) -> str:
    return f"{(system or _platform.system()) or '?'} {(machine or _platform.machine()) or '?'}"


def unsupported_hint(system: str | None = None, machine: str | None = None) -> str:
    """The clause the setup gap uses when there's nothing to download for this host."""
    return f"agent-browser publishes no build for this platform ({describe_platform(system, machine)}) to download"


def fetch_spec(platform: str | None = None) -> FetchSpec | None:
    key = platform or platform_key()
    if key is None or key not in ASSETS:
        return None
    asset, sha256 = ASSETS[key]
    return FetchSpec(CLI_VERSION, key, asset, RELEASE_URL.format(version=CLI_VERSION, asset=asset), sha256)


# ── where it lands ────────────────────────────────────────────────────────────────
def binary_name(platform: str | None = None) -> str:
    key = platform or platform_key() or ("win32" if os.name == "nt" else "")
    return "agent-browser.exe" if key.startswith("win32") else "agent-browser"


def cache_root() -> Path:
    """``AGENT_BROWSER_CLI_DIR`` if set, else ``instance_paths().cache_dir / "agent-browser"``
    — the host's box-tier cache (see the module doc for why box, not instance)."""
    raw = os.environ.get(ENV_CLI_DIR, "").strip()
    if raw:
        return Path(raw).expanduser()
    from infra.paths import instance_paths

    return Path(instance_paths().cache_dir) / "agent-browser"


def fetched_path(platform: str | None = None, *, base: Path | None = None, version: str = CLI_VERSION) -> Path:
    """``<cache>/<version>/agent-browser[.exe]`` — keyed by version, so a pin bump fetches
    the new release rather than resolving the old one."""
    return (base or cache_root()) / version / binary_name(platform)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def installed_path(platform: str | None = None, *, base: Path | None = None) -> str:
    """The fetched CLI's path when it is present AND is the pinned asset, else ``""``.

    Every browser command resolves the CLI, and hashing ~10 MB costs ~20 ms, so the verdict
    is cached per (path, size, mtime): a file replaced behind our back gets a new signature
    and is re-hashed — and ignored unless it's still the pinned build."""
    spec = fetch_spec(platform)
    if spec is None:
        return ""
    path = fetched_path(spec.platform, base=base)
    try:
        st = path.stat()
        if not path.is_file():
            return ""
    except OSError:
        return ""
    sig = (str(path), st.st_size, st.st_mtime_ns)
    holder = _slot()
    with holder.lock:
        verdict = holder.verified.get(sig)
    if verdict is None:
        try:
            verdict = _sha256_file(path) == spec.sha256
        except OSError:
            return ""
        with holder.lock:
            if len(holder.verified) > 32:
                holder.verified.clear()
            holder.verified[sig] = verdict
    if not verdict:
        return ""
    if os.name != "nt" and not os.access(path, os.X_OK):
        return ""
    return str(path)


# ── process-stable fetch state ────────────────────────────────────────────────────
# idle → fetching → done | failed ; or unsupported (no build for this host).
_SLOT_NAME = "agent_browser.cli_fetch::state"


def _fresh_state() -> dict:
    return {"state": "idle", "path": "", "error": "", "started": 0.0, "finished": 0.0,
            "version": CLI_VERSION, "platform": ""}


def _slot():
    holder = sys.modules.get(_SLOT_NAME)
    if holder is None:
        holder = types.ModuleType(_SLOT_NAME)
        holder.__doc__ = "Process-stable holder for agent_browser's CLI fetch state — data, not code."
        holder.state = _fresh_state()
        holder.lock = threading.Lock()
        holder.idle = threading.Event()  # set ⇔ no fetch in flight (waiters block on it)
        holder.idle.set()
        holder.verified = {}
        holder = sys.modules.setdefault(_SLOT_NAME, holder)  # atomic install
    return holder


def fetch_state() -> dict:
    """A copy of ``{state, path, error, started, finished, version, platform}``."""
    holder = _slot()
    with holder.lock:
        return dict(holder.state)


def reset_state() -> None:
    """Tests only — back to idle, verdict cache cleared."""
    holder = _slot()
    with holder.lock:
        holder.state = _fresh_state()
        holder.verified.clear()
        holder.idle.set()


def _finish(*, release: bool = False, **fields) -> None:
    holder = _slot()
    with holder.lock:
        holder.state.update(finished=time.time(), **fields)
        if release:
            holder.idle.set()


# ── the download ──────────────────────────────────────────────────────────────────
def _egress_check(url: str) -> str | None:
    """The host's in-process egress allowlist verdict for ``url`` (ADR 0008), or None when
    allowed / the guard is unavailable."""
    try:
        from security.egress import check_url
    except Exception:  # noqa: BLE001 — an older host
        return None
    try:
        return check_url(url)
    except Exception:  # noqa: BLE001 — a guard that errors must not block the fetch
        return None


def check_redirect_target(newurl: str) -> None:
    """A redirect hop is allowed only to HTTPS on ``*.githubusercontent.com`` AND past the
    host's egress allowlist. Raises ``PermissionError`` otherwise."""
    parts = urllib.parse.urlsplit(newurl)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(REDIRECT_HOST_SUFFIX):
        raise PermissionError(f"redirect to {parts.scheme}://{host or '?'} refused — only https *{REDIRECT_HOST_SUFFIX}")
    blocked = _egress_check(newurl)
    if blocked:
        raise PermissionError(f"egress blocked on redirect: {blocked}")


class _PinnedRedirects(urllib.request.HTTPRedirectHandler):
    """urllib's redirect handler with every hop run through ``check_redirect_target``."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_redirect_target(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urllib_download(url: str, timeout: float) -> bytes:
    """Plain HTTPS GET → bytes, bounded by ``timeout`` overall (checked between chunks; the
    socket timeout is 30 s per operation) and by ``MAX_ASSET_BYTES``."""
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, headers={"User-Agent": "protoagent-agent-browser/cli-fetch"})
    buf = io.BytesIO()
    opener = urllib.request.build_opener(_PinnedRedirects())
    with opener.open(req, timeout=max(1.0, min(30.0, timeout))) as resp:  # noqa: S310 — pinned https URL
        while True:
            if deadline - time.monotonic() <= 0:
                raise TimeoutError(f"download exceeded {timeout:.0f}s")
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            buf.write(chunk)
            if buf.tell() > MAX_ASSET_BYTES:
                raise ValueError(f"asset larger than {MAX_ASSET_BYTES} bytes — refusing")
    return buf.getvalue()


def _sweep_stale_temps(folder: Path, *, now: float | None = None) -> None:
    """Remove temps a dead install left behind — only OLD ones (see ``STALE_TEMP_S``).
    ``st_mtime`` (not ``st_ctime``, which is creation time on Windows)."""
    now = time.time() if now is None else now
    for stale in folder.glob(_TEMP_PREFIX + "*"):
        try:
            if now - stale.stat().st_mtime > STALE_TEMP_S:
                stale.unlink()
        except OSError:
            pass


def _replace(tmp: str, dest: Path, sha256: str, *, sleep=time.sleep, replace=None) -> None:
    """``os.replace(tmp, dest)`` with Windows' in-use trap handled: retry a
    ``PermissionError`` with backoff; if it never clears, accept a ``dest`` that already
    holds the pinned bytes (dropping ``tmp``) and raise otherwise. ``sleep``/``replace`` are
    injectable so the retry path is testable on any OS."""
    replace = replace or os.replace
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            replace(tmp, dest)
            return
        except PermissionError:
            if attempt + 1 < _REPLACE_ATTEMPTS:
                sleep(_REPLACE_BACKOFF_S * (2**attempt))
    try:
        same = dest.is_file() and _sha256_file(dest) == sha256
    except OSError:
        same = False
    if same:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return
    raise PermissionError(f"could not move the verified CLI into place at {dest} (is it in use?)")


def install(spec: FetchSpec, dest: Path, *, downloader=None, timeout: float = FETCH_TIMEOUT_S) -> Path:
    """Download ``spec.url``, refuse it unless its sha256 is ``spec.sha256``, then install
    it at ``dest`` atomically (temp file + ``os.replace``, 0755 on POSIX). Returns ``dest``.
    Raises on any failure — and on every failure path nothing is left at ``dest`` that
    wasn't there before, and no temp is left beside it."""
    blocked = _egress_check(spec.url)
    if blocked:
        raise PermissionError(f"egress blocked: {blocked}")
    data = (downloader or _urllib_download)(spec.url, timeout=timeout)
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"downloader returned {type(data).__name__}, expected bytes")
    if len(data) > MAX_ASSET_BYTES:
        raise ValueError(f"asset larger than {MAX_ASSET_BYTES} bytes — refusing")
    digest = hashlib.sha256(data).hexdigest()
    if digest != spec.sha256:
        raise ChecksumError(
            f"sha256 mismatch for {spec.asset} v{spec.version}: expected {spec.sha256[:12]}…, "
            f"got {digest[:12]}… — not installing it"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_temps(dest.parent)
    fd, tmp = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=".part", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o755)
        _replace(tmp, dest, spec.sha256)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def _run_fetch(spec: FetchSpec, dest: Path, downloader, timeout: float, on_done) -> None:
    """One attempt. ``on_done`` runs BEFORE joined waiters are released, so a tool call that
    joined this download resumes against a banner that already says what happened."""
    try:
        try:
            path = install(spec, dest, downloader=downloader, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — every failure is a setup-gap message, never a traceback
            _finish(state="failed", error=f"{type(exc).__name__}: {exc}")
            log.warning("[agent_browser] agent-browser v%s download failed: %s", spec.version, exc)
        else:
            _finish(state="done", path=str(path), error="")
            log.info("[agent_browser] agent-browser v%s (%s) verified and installed at %s", spec.version,
                     spec.platform, path)
        if callable(on_done):
            try:
                on_done()
            except Exception:  # noqa: BLE001 — a banner refresh must never break the fetch
                log.exception("[agent_browser] refreshing the setup gap after the CLI fetch failed")
    finally:
        _slot().idle.set()


def ensure_cli(
    *,
    background: bool = True,
    force: bool = False,
    wait: float | None = None,
    downloader=None,
    platform: str | None = None,
    base: Path | None = None,
    timeout: float = FETCH_TIMEOUT_S,
    on_done=None,
) -> dict:
    """Make sure the pinned CLI is in the cache. Never raises; returns ``fetch_state()``.

    * already there and verified → ``done``, no download;
    * no build for this platform → ``unsupported`` (``error`` says why);
    * a fetch already in flight → joined (for up to ``wait`` seconds), never a second one;
    * ``failed`` STAYS failed for the automatic path (``force=False``), so a tool call can't
      re-download on every command after one failure — the banner's button passes
      ``force=True`` to retry.

    ``background=True`` downloads on a daemon thread and returns at once (the banner
    button); ``False`` downloads inline (a tool call's first use, already off the event
    loop). ``on_done`` runs after an attempt either way — the plugin passes its gap refresh,
    so the banner moves on without a restart.
    """
    holder = _slot()
    try:
        spec = fetch_spec(platform)
        if spec is None:
            with holder.lock:
                if holder.state["state"] != "fetching":
                    holder.state.update(state="unsupported", error=unsupported_hint(), platform="")
            return fetch_state()
        existing = installed_path(spec.platform, base=base)
        with holder.lock:
            current = holder.state["state"]
            if existing and current != "fetching":
                holder.state.update(state="done", path=existing, error="", platform=spec.platform)
                return dict(holder.state)
            if current == "failed" and not force:
                return dict(holder.state)
            start = current != "fetching"
            if start:
                holder.state.update(state="fetching", path="", error="", started=time.time(), finished=0.0,
                                    platform=spec.platform)
                holder.idle.clear()
        if start:
            dest = fetched_path(spec.platform, base=base)
            log.info("[agent_browser] no agent-browser CLI — fetching v%s for %s from %s", spec.version,
                     spec.platform, spec.url)
            if background:
                threading.Thread(target=_run_fetch, args=(spec, dest, downloader, timeout, on_done),
                                 name="agent-browser-cli-fetch", daemon=True).start()
            else:
                _run_fetch(spec, dest, downloader, timeout, on_done)
        if wait:
            holder.idle.wait(wait)
    except Exception as exc:  # noqa: BLE001 — belt and braces: never raise into a tool call
        _finish(state="failed", error=f"{type(exc).__name__}: {exc}", release=True)
        log.warning("[agent_browser] the CLI fetch could not start: %s", exc, exc_info=True)
    return fetch_state()
