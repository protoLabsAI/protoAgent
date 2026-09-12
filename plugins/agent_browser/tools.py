"""The browser tools — thin subprocess wrappers over the `agent-browser` CLI.

agent-browser (vercel-labs) is a native-Rust CLI + daemon: each invocation talks to
a persistent browser session, so these tools are stateless shells. The model's loop
is **open → snapshot → act on an `@eN` ref → verify**; `snapshot` returns the
accessibility tree with the refs the other tools consume.

Tools return the CLI's stdout (refs, extracted text, file paths) for the model to
read, and degrade to a readable ``Error: …`` string rather than raising — a failed
browser action should inform the loop, not crash it.

Three things the in-tree copy adds (#3451):

* **A broken setup is an operator banner, not just a tool-loop error.** Any failing run —
  ``FileNotFoundError`` (no CLI) *or* a non-zero exit (most often "a CLI with no Chrome to
  drive") — asks ``preflight`` to re-report, and the next clean run clears it, so the
  banner appears and self-heals mid-session (``graph/plugins/setup_gaps.py``). Only on a
  CHANGE of state, so a failing loop doesn't run a probe per call.
* **Captures are fenced, and only claimed when they exist.** ``browser_screenshot`` /
  ``browser_pdf`` resolve their path inside this plugin's own instance store
  (``storage.resolve_capture_path``) and refuse an escape — what the manifest's
  ``filesystem: scoped`` claim had been asserting without enforcing — and the file is
  stat'd before the tool says "Saved to", because the CLI can exit 0 having written
  nothing.
* **A model-supplied argv element may not start with ``-``.** The CLI reads options
  anywhere in the command, so such a value is silently swallowed as a flag (verified on
  0.27.1: ``fill '#q' '--help'`` prints help and fills nothing) — a correctness bug first
  and an injection shape second.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading

from langchain_core.tools import tool

from . import preflight, storage
from .runtime import launch_flags

log = logging.getLogger("protoagent.plugins.agent_browser")

# Default aggregate stdout+stderr byte cap when `max_response_bytes` is unset.
_DEFAULT_MAX_RESPONSE_BYTES = 200_000
_READ_CHUNK = 65_536


def get_browser_tools(cfg: dict | None, refresh_gaps=None):
    """Build the browser toolset.

    ``refresh_gaps`` (optional) is called with no arguments when a run FAILS in a way a
    setup gap could explain, and once more on the first success afterwards —
    ``register()`` passes a closure over ``preflight.report``, so the operator banner
    tracks reality without a probe on every call. Omitted in unit tests, where the tools
    stay host-free.
    """
    cfg = cfg or {}
    binary = str(cfg.get("binary") or "agent-browser")
    timeout = float(cfg.get("timeout_s", 60))
    # Plugin-owned cap on the total bytes a single invocation may buffer. Untrusted page
    # content (get text/html, eval) can emit unbounded output that would otherwise pile up
    # in memory and flood the model's context window, so we read the pipes incrementally
    # and stop the child the moment the aggregate crosses the cap.
    # A non-positive/garbage configured value would make EVERY command fail "output
    # exceeded -1 bytes", so it falls back to the default rather than bricking the toolset.
    try:
        max_bytes = int(cfg.get("max_response_bytes", _DEFAULT_MAX_RESPONSE_BYTES))
    except (TypeError, ValueError):
        max_bytes = _DEFAULT_MAX_RESPONSE_BYTES
    if max_bytes <= 0:
        if cfg.get("max_response_bytes") not in (None, "", 0):
            log.warning("[agent_browser] ignoring max_response_bytes=%r (must be > 0); using %d",
                        cfg.get("max_response_bytes"), _DEFAULT_MAX_RESPONSE_BYTES)
        max_bytes = _DEFAULT_MAX_RESPONSE_BYTES
    degraded = threading.Event()  # the last run failed a setup-explicable way → re-check

    def _gap_changed(*, degrade: bool) -> None:
        """Ask the host to re-probe — but only on a CHANGE of state, so a failing loop
        doesn't run a preflight per call."""
        if degrade:
            if degraded.is_set():
                return          # already reported; don't re-probe once per failing call
            degraded.set()
        else:
            if not degraded.is_set():
                return          # steady-state success: nothing to clear
            degraded.clear()
        if not callable(refresh_gaps):
            return
        try:
            refresh_gaps()
        except Exception:  # noqa: BLE001 — a banner refresh must never fail a tool call
            log.exception("[agent_browser] refreshing the setup gap failed")

    def _run(*args: str) -> str:
        """Run `agent-browser <args>` and return stdout, or a readable error.

        Uses Popen with one drain thread per pipe (concurrent, so a full stderr can't
        deadlock stdout) enforcing an aggregate byte cap owned here. On overflow the child
        is killed and a bounded diagnostic is returned; on timeout the child is terminated.
        Either way the child is reaped — no zombies.
        """
        try:
            proc = subprocess.Popen([binary, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            # Surface it to the OPERATOR too, not just into the model's loop: the console
            # showed `warnings: []` while every browser call failed, which is the gap the
            # setup-gap seam exists for.
            _gap_changed(degrade=True)
            return (f"Error: {binary!r} not on PATH — install it: "
                    f"`{preflight.INSTALL_HINT}`. The console's setup banner now says so too.")

        out_buf, err_buf = bytearray(), bytearray()
        total = 0
        overflow = False
        lock = threading.Lock()

        def _drain(pipe, buf: bytearray) -> None:
            nonlocal total, overflow
            hit = False
            try:
                for block in iter(lambda: pipe.read(_READ_CHUNK), b""):
                    with lock:
                        if overflow:
                            break
                        total += len(block)
                        if total > max_bytes:  # aggregate crossed the cap → stop + kill
                            overflow = True
                            hit = True
                            break
                        buf.extend(block)
            except (OSError, ValueError):
                pass  # pipe closed under us (e.g. after kill) — nothing more to read
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass
            if hit:
                proc.kill()  # unblock the sibling reader and let wait() return

        drains = [threading.Thread(target=_drain, args=(proc.stdout, out_buf), daemon=True),
                  threading.Thread(target=_drain, args=(proc.stderr, err_buf), daemon=True)]
        for t in drains:
            t.start()

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()  # terminate …
            try:
                proc.wait(timeout=5)  # … and reap, so we never leave a zombie
            except subprocess.TimeoutExpired:
                pass
        # KNOWN LIMITATION (vendored from the standalone repo's #20, tracked for a
        # follow-up): the kill above reaps only the DIRECT child. If that child left a
        # grandchild holding the pipes, the drain threads stay blocked in `pipe.read()`
        # and this join never returns — leaking the `asyncio.to_thread` worker for the
        # life of the process. Reproducible with a stand-in CLI that backgrounds a
        # `sleep`; the real agent-browser 0.27.1 does NOT do it (a killed child releases
        # the pipes). The fix is `start_new_session=True` + a process-GROUP kill, or a
        # bounded join — deliberately not folded into this import, which is already
        # carrying four behaviour changes.
        for t in drains:
            t.join()

        if timed_out:
            return f"Error: `agent-browser {' '.join(args)}` timed out after {timeout:g}s"
        if overflow:
            return f"Error: output exceeded {max_bytes} bytes (truncated)"
        out = out_buf.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            err = (err_buf.decode("utf-8", "replace") or out or "").strip()
            # A non-zero exit can ALSO be a setup gap the boot-time preflight didn't catch
            # — most often "the CLI is here but has no Chrome to drive", which never raises
            # FileNotFoundError. Re-probe so that banner appears (and clears) mid-session
            # too, instead of only on the CLI-missing path.
            _gap_changed(degrade=True)
            return f"Error: `agent-browser {' '.join(args)}` failed: {err[:500]}"
        _gap_changed(degrade=False)  # a clean run proves the setup works — clear any banner
        return out or "(ok)"

    async def _ab(*args: str) -> str:
        return await asyncio.to_thread(_run, *args)

    def _bad_operand(**values: str) -> str | None:
        """Reject a model-supplied argv element that the CLI would read as an OPTION.

        The CLI scans for options across the WHOLE argv, not just before the positionals
        — verified against 0.27.1: ``fill '#q' '--help'`` prints help instead of filling,
        and so does ``press '--help'``. So a value starting with ``-`` is (a) a silent
        no-op today, which is the common case and a plain correctness bug, and (b) a way
        for model- or page-chosen text to set a launch flag (``--auto-connect``,
        ``--allow-file-access`` are single-element). Not exploitable as shipped — no CDP
        port is opened — but there is no legitimate URL, selector or key that starts with
        ``-``, and a refusal beats a silent no-op even for free text. The CLI has no
        ``--`` end-of-options escape (``open -- --help`` still prints help), so refusing
        is the only lever. Returns an error string, or None."""
        for what, value in values.items():
            if str(value).startswith("-"):
                return (f"Error: {what} may not start with '-' — the agent-browser CLI reads it "
                        f"as an option anywhere in the command (it would silently do nothing). "
                        f"Got {str(value)[:80]!r}.")
        return None

    async def _capture(verb: str, path: str, default_name: str) -> str:
        """Run a file-producing command (``screenshot`` / ``pdf``) inside the fence.

        The fence is resolved BEFORE the subprocess, so a rejected path never reaches
        Chrome, and the tool reports the absolute file on success — the handoff the
        artifact plugin's ``save_file_artifact`` takes.

        **"Saved to …" is only said once the bytes are on disk.** The CLI can exit 0 having
        written nothing (no page open, a swallowed renderer error), and reporting that as a
        success sent the agent on to ``save_file_artifact``, which answered "No file at … —
        write the file first": an undiagnosable dead end two tools away from the cause. A
        zero-byte file was worse — stored, then downloaded as a broken PDF. So the file is
        stat'd, and a partial file left behind by a failed run is removed rather than left
        for the next call to report as a success.
        """
        try:
            target = await asyncio.to_thread(
                storage.resolve_capture_path, path,
                default_name=storage.unique_default_name(default_name),
            )
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001 — an unwritable store informs the loop
            return f"Error: could not prepare a capture directory: {e}"
        existed = target.exists()
        out = await _ab(verb, str(target))
        failed = out.startswith("Error:")
        if not failed:
            try:
                size = target.stat().st_size
            except OSError:
                size = -1
            if size <= 0:
                failed = True
                out = (f"Error: `agent-browser {verb}` reported success but wrote "
                       f"{'an empty file' if size == 0 else 'no file'} — is a page open? "
                       f"Call browser_open first, then retry.")
        if failed:
            if not existed:  # don't leave a partial/empty file to be reported as a hit later
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            return out
        await asyncio.to_thread(storage.prune_captures)
        note = ""
        if size > storage.ARTIFACT_BLOB_LIMIT_BYTES:
            # Say it HERE: save_file_artifact would reject it, and the agent would have no
            # way to connect that refusal back to this capture.
            note = (f"\nNote: {size // (1024 * 1024)} MB exceeds save_file_artifact's default "
                    f"{storage.ARTIFACT_BLOB_LIMIT_BYTES // (1024 * 1024)} MB limit — raise the "
                    f"artifact plugin's max_blob_kb, or capture a smaller page.")
        body = f"{out}\nSaved to {target}" if out and out != "(ok)" else f"Saved to {target}"
        return body + note

    # ── navigation ────────────────────────────────────────────────────────────
    @tool
    async def browser_open(url: str = "") -> str:
        """Launch the browser (or navigate the current session). Pass a `url` to go
        there, or leave blank to open about:blank. Start every browsing task here.
        The configured runtime options (headed/profile/device/allowed_domains/stealth/…)
        are applied here, where the session launches."""
        if (bad := _bad_operand(url=url)):
            return bad
        flags = launch_flags(cfg)
        return await _ab(*flags, "open", url) if url else await _ab(*flags, "open")

    @tool
    async def browser_back() -> str:
        """Navigate back in history."""
        return await _ab("back")

    @tool
    async def browser_forward() -> str:
        """Navigate forward in history."""
        return await _ab("forward")

    @tool
    async def browser_reload() -> str:
        """Reload the current page."""
        return await _ab("reload")

    # ── perception ────────────────────────────────────────────────────────────
    @tool
    async def browser_snapshot() -> str:
        """Get the page's accessibility tree with compact `@eN` element refs. Call
        this before clicking/filling — the refs (e.g. `@e2`) are what the action
        tools target. The canonical way to 'see' the page for the agent."""
        return await _ab("snapshot")

    @tool
    async def browser_get_text(selector: str = "body") -> str:
        """Get the visible text of an element (a `@eN` ref or a CSS selector;
        defaults to the whole `body`). Use to extract/read page content."""
        return _bad_operand(selector=selector) or await _ab("get", "text", selector)

    @tool
    async def browser_get_html(selector: str = "") -> str:
        """Get the HTML of an element (a `@eN` ref or CSS selector), or the page."""
        if (bad := _bad_operand(selector=selector)):
            return bad
        return await _ab("get", "html", selector) if selector else await _ab("get", "html")

    @tool
    async def browser_get_value(selector: str) -> str:
        """Get the current value of a form field (a `@eN` ref or CSS selector)."""
        return _bad_operand(selector=selector) or await _ab("get", "value", selector)

    # ── interaction ───────────────────────────────────────────────────────────
    @tool
    async def browser_click(selector: str) -> str:
        """Click an element by `@eN` ref (from `browser_snapshot`) or a CSS selector
        (e.g. `#submit`). Snapshot first to get the ref."""
        return _bad_operand(selector=selector) or await _ab("click", selector)

    @tool
    async def browser_fill(selector: str, text: str) -> str:
        """Clear a field and fill it with `text` (a `@eN` ref or CSS selector)."""
        return _bad_operand(selector=selector, text=text) or await _ab("fill", selector, text)

    @tool
    async def browser_type(selector: str, text: str) -> str:
        """Type `text` into an element without clearing it first (a ref or selector)."""
        return _bad_operand(selector=selector, text=text) or await _ab("type", selector, text)

    @tool
    async def browser_press(key: str) -> str:
        """Press a key or chord on the focused element (e.g. `Enter`, `Tab`,
        `Control+a`)."""
        return _bad_operand(key=key) or await _ab("press", key)

    @tool
    async def browser_hover(selector: str) -> str:
        """Hover the pointer over an element (a `@eN` ref or CSS selector)."""
        return _bad_operand(selector=selector) or await _ab("hover", selector)

    @tool
    async def browser_eval(expression: str) -> str:
        """Evaluate a JavaScript `expression` in the page and return the result.
        Use sparingly — prefer snapshot + the action tools. Wrap a leading `-` in
        parentheses (`(-1)`) — the CLI would read it as an option."""
        return _bad_operand(expression=expression) or await _ab("eval", expression)

    # ── capture + session ─────────────────────────────────────────────────────
    @tool
    async def browser_screenshot(path: str = "") -> str:
        """Save a PNG screenshot of the current page. Returns the absolute file path —
        hand that to `save_file_artifact` to put the image in the Artifact panel.

        `path` is an optional FILENAME or relative path (`shots/home.png`); leave it blank
        and the file is named for you, which is what you want when you just need the image
        (two unnamed captures never overwrite each other). Files land in this plugin's own
        capture directory; an absolute path outside it is refused."""
        return await _capture("screenshot", path, "page.png")

    @tool
    async def browser_pdf(path: str = "") -> str:
        """Print the current page to PDF (Chrome's print-to-PDF) and return the absolute
        file path. This is the HTML→PDF path: open a page — or an artifact/report you
        rendered — then `browser_pdf`, then hand the file to `save_file_artifact` so the
        user can download it.

        `path` is an optional FILENAME or relative path (`out/resume.pdf`); leave it blank
        and the file is named for you (two unnamed captures never overwrite each other).
        Files land in this plugin's own capture directory; an absolute path outside it is
        refused."""
        return await _capture("pdf", path, "page.pdf")

    @tool
    async def browser_close() -> str:
        """Close the browser session. Call when the task is done to free the daemon."""
        return await _ab("close")

    return [
        browser_open, browser_back, browser_forward, browser_reload,
        browser_snapshot, browser_get_text, browser_get_html, browser_get_value,
        browser_click, browser_fill, browser_type, browser_press, browser_hover,
        browser_eval, browser_screenshot, browser_pdf, browser_close,
    ]
