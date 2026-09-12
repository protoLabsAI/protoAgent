"""The browser tools — thin subprocess wrappers over the `agent-browser` CLI.

agent-browser (vercel-labs) is a native-Rust CLI + daemon: each invocation talks to
a persistent browser session, so these tools are stateless shells. The model's loop
is **open → snapshot → act on an `@eN` ref → verify**; `snapshot` returns the
accessibility tree with the refs the other tools consume.

Tools return the CLI's stdout (refs, extracted text, file paths) for the model to
read, and degrade to a readable ``Error: …`` string rather than raising — a failed
browser action should inform the loop, not crash it.

Three things the in-tree copy adds (#3451):

* **A broken setup is an operator banner, not just a tool-loop error.** The tools start
  in the state the boot probe found, re-probe on a failing run (``FileNotFoundError``
  always; a non-zero exit at most once a minute, since a missed click exits non-zero too)
  and on a success while a banner is up — so the banner appears mid-session and clears on
  the next good call, with no restart (``graph/plugins/setup_gaps.py``).
* **Captures are fenced, and only claimed when they exist.** ``browser_screenshot`` /
  ``browser_pdf`` resolve their path inside this plugin's own instance store
  (``storage.resolve_capture_path``) and refuse an escape — what the manifest's
  ``filesystem: scoped`` claim had been asserting without enforcing — and the file is
  stat'd before the tool says "Saved to", because the CLI can exit 0 having written
  nothing.
* **A model-supplied argv element may not look like a CLI option** (a dash, then a
  letter: ``--headed``, ``-h``). The CLI reads options anywhere in the command, so such a
  value is swallowed as a flag — and ``--headed`` / ``--allow-file-access`` take effect on
  a first launch. Everything else (``-5``, ``-$50.00``, ``- buy milk``, a lone ``-``) goes
  through untouched, so negative amounts and the minus key still work.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import threading
import time

from langchain_core.tools import tool

from . import preflight, storage
from .runtime import launch_flags

log = logging.getLogger("protoagent.plugins.agent_browser")

# Default aggregate stdout+stderr byte cap when `max_response_bytes` is unset.
_DEFAULT_MAX_RESPONSE_BYTES = 200_000
_READ_CHUNK = 65_536

# Setup re-probe rate limits (see _recheck). A probe is ~40 ms, but it is two subprocesses,
# and a failing agent loop can fire several calls a second.
_RECHECK_AFTER_FAILURE_S = 60.0
_RECHECK_AFTER_SUCCESS_S = 10.0

# The CLI's option grammar — `-h`, `--help`, `--headed`, … : a dash, optionally another,
# then a LETTER. See _bad_operand for why this, and not "any leading dash".
_OPTION_LIKE = re.compile(r"^--?[A-Za-z]")
_WORKAROUND = {
    "text": ("To enter it anyway, set the field from JavaScript with browser_eval, e.g. "
             "document.querySelector('#q').value = '--foo' (then dispatch an 'input' event if "
             "the page reacts to typing)."),
    "key": "Key names never start that way; for the minus key pass '-' (or 'Minus').",
    "expression": "Wrap it in parentheses, e.g. (-a) instead of -a.",
    "*": "No URL, selector or @ref starts that way — check the value.",
}

# "Does this page have anything on it?" — 1 or 0. The URL alone can't say: an agent can open
# a blank page, write a report into it with browser_eval and print THAT, all at about:blank.
_PAGE_HAS_CONTENT_JS = "+!!(document.body && (document.body.innerText.trim() || document.body.children.length))"


def get_browser_tools(cfg: dict | None, refresh_gaps=None, *, start_gap: bool = False):
    """Build the browser toolset.

    ``refresh_gaps`` (optional) re-probes the setup and re-reports the banner. It should
    return the ``preflight.Probe``, so the tools know whether a banner is ACTUALLY up
    (``register()`` passes a closure over ``preflight.report``). ``start_gap`` is what the
    boot probe found: without it, a banner raised at boot was never cleared if the operator
    fixed the setup before any call failed. Both are omitted in unit tests, where the tools
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
    # What the operator's banner currently says (seeded by the boot probe), and when each
    # direction last re-probed. See _recheck.
    gap = {"up": bool(start_gap), "fail_at": float("-inf"), "ok_at": float("-inf")}
    gap_lock = threading.Lock()

    def _recheck(*, failed: bool, force: bool = False) -> None:
        """Re-probe the setup when a call's outcome disagrees with the banner.

        * A FAILED call while no banner is up might be a new gap — but a missed click or a
          stale ``@ref`` exits non-zero too, so this direction runs at most once per
          ``_RECHECK_AFTER_FAILURE_S``. ``force`` bypasses that: ``FileNotFoundError`` is
          unambiguous.
        * A SUCCESSFUL call while a banner is up is evidence the setup was fixed — re-probe
          so the banner clears (rate-limited too, in case the probe still disagrees).

        The new state comes from the probe itself when ``refresh_gaps`` returns one, so a
        routine failure the probe finds harmless doesn't leave the tools marked broken.
        """
        now = time.monotonic()
        with gap_lock:
            if failed:
                if gap["up"] or (not force and now - gap["fail_at"] < _RECHECK_AFTER_FAILURE_S):
                    return
                gap["fail_at"] = now
            else:
                if not gap["up"] or now - gap["ok_at"] < _RECHECK_AFTER_SUCCESS_S:
                    return
                gap["ok_at"] = now
        result = None
        if callable(refresh_gaps):
            try:
                result = refresh_gaps()
            except Exception:  # noqa: BLE001 — a banner refresh must never fail a tool call
                log.exception("[agent_browser] refreshing the setup gap failed")
                return
        with gap_lock:
            # A probe says what the banner now shows; without one (no host), take the call's
            # outcome as the state.
            gap["up"] = bool(preflight.hint(result)) if isinstance(result, preflight.Probe) else failed

    def _launch_error(e: OSError) -> str:
        """Two problems raise from Popen and one of them is misleading: a binary that ISN'T
        there, and one that is but the kernel won't start — the npm launcher
        (`#!/usr/bin/env node`) with no node on this process's PATH raises the SAME
        FileNotFoundError, and a non-executable file raises PermissionError. "Install it"
        is the wrong advice for the second, so tell them apart."""
        found = preflight.resolve_binary(binary)
        if isinstance(e, FileNotFoundError) and not found:
            return (f"Error: {binary!r} not on PATH — install it: "
                    f"`{preflight.INSTALL_HINT}`. The console's setup banner now says so too.")
        reason = e.strerror or str(e) or e.__class__.__name__
        where = f" at {found}" if found else ""
        return (f"Error: {binary!r} was found{where} but could not be started ({reason}). If it's "
                f"the npm launcher script, its interpreter (node) isn't on this process's PATH — "
                f"point the plugin's `binary` setting at the native agent-browser binary, or put "
                f"node on PATH. The console's setup banner says so too.")

    def _run(*args: str) -> str:
        """Run `agent-browser <args>` and return stdout, or a readable error.

        Uses Popen with one drain thread per pipe (concurrent, so a full stderr can't
        deadlock stdout) enforcing an aggregate byte cap owned here. On overflow the child
        is killed and a bounded diagnostic is returned; on timeout the child is terminated.
        Either way the child is reaped — no zombies.
        """
        try:
            proc = subprocess.Popen([binary, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            # Surface it to the OPERATOR too, not just into the model's loop: the console
            # showed `warnings: []` while every browser call failed, which is the gap the
            # setup-gap seam exists for.
            _recheck(failed=True, force=True)
            return _launch_error(e)

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
            _recheck(failed=True)
            return f"Error: `agent-browser {' '.join(args)}` failed: {err[:500]}"
        _recheck(failed=False)  # a clean run is evidence the setup works — clear any banner
        return out or "(ok)"

    async def _ab(*args: str) -> str:
        return await asyncio.to_thread(_run, *args)

    def _bad_operand(**values: str) -> str | None:
        """Reject a model-supplied argv element the CLI would take as one of ITS options.

        The CLI scans the whole argv for options — verified on 0.27.1: ``fill '#q'
        '--help'`` prints help instead of filling, and ``--headed`` / ``--allow-file-access``
        in a value would change how a first launch happens, so model- or page-chosen text
        could set a launch flag. But it only swallows what LOOKS like an option: ``-5``,
        ``-$50.00``, ``- buy milk``, a lone ``-`` or ``--`` all go through untouched, and
        ``eval '-1'`` returns -1. So the rule is the option grammar — a dash, then a letter
        (``_OPTION_LIKE``) — not "starts with a dash"; the first version refused every
        leading dash and blocked negative amounts and the minus key.

        Deliberately the grammar, not the CLI's current option list: an option upstream adds
        tomorrow is refused today. The price is that flag-shaped text the CLI happens not to
        know (``--foo``, ``-x=1``) is refused too, and the message says how to enter it
        anyway. The CLI has no ``--`` end-of-options escape (``open -- --help`` still prints
        help), so refusing is the only lever. Returns an error string, or None.
        """
        for what, value in values.items():
            text = str(value)
            if _OPTION_LIKE.match(text):
                return (f"Error: {what} {text[:80]!r} looks like a command-line option (a dash and "
                        f"then a letter), and the agent-browser CLI reads options anywhere in a "
                        f"command, so it would be taken as a flag rather than as your {what}. "
                        f"{_WORKAROUND.get(what, _WORKAROUND['*'])}")
        return None

    async def _capture(verb: str, path: str, default_name: str) -> str:
        """Run a file-producing command (``screenshot`` / ``pdf``) inside the fence.

        The fence is resolved BEFORE the subprocess, so a rejected path never reaches
        Chrome, and the tool reports the absolute file on success — the handoff the artifact
        plugin's ``save_file_artifact`` takes.

        **"Saved to …" means THIS call's bytes are on disk, and the previous file is never
        at risk.** The CLI writes a short temp name beside the target (``storage.temp_path_for``);
        only a finished, non-empty temp is swapped into place with one ``os.replace``, and on
        any failure — including the turn being cancelled — just the temp is dropped. So:

        * a run that exits 0 having written nothing (or zero bytes) is an error, and any
          previous file with that name is untouched;
        * a cancelled turn or a ``kill -9`` can leave at most a disposable temp (swept by the
          next prune once it's an hour old) — never a missing target;
        * two exports racing for one name: the last to FINISH wins, and neither deletes the
          other's output;
        * the target is marked in flight, so a concurrent capture's prune can't take it.

        A blank page is refused up front: with nothing open the CLI still exits 0 and prints a
        blank ~860-byte PDF of ``about:blank``. But ``about:blank`` isn't necessarily EMPTY —
        an agent can write a report into it with ``browser_eval`` — so the check is on the
        page's content, and the URL only decides whether that check is needed.
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

        current = await _ab("get", "url")
        if current.startswith("Error:"):
            return current
        if (current.splitlines() or [""])[0].strip() == "about:blank":
            has = await _ab("eval", _PAGE_HAS_CONTENT_JS)
            if (has.splitlines() or [""])[0].strip() != "1":
                return ("Error: the page is blank (about:blank, with nothing on it), so there is "
                        "nothing to capture. Open a page with browser_open first. If you have HTML "
                        "rather than a URL, open it as a data:text/html,… URL (URL-encoded) or save "
                        "it to a file and open its file:// URL — or write it into this blank page "
                        f"with browser_eval — then call browser_{verb} again.")

        with storage.in_flight(target):
            temp = storage.temp_path_for(target)
            try:
                out = await _ab(verb, str(temp))
            except BaseException:
                # Cancelled mid-capture: the target was never touched. Drop our temp; if the
                # CLI writes it after we've gone, the next prune sweeps the orphan.
                storage.discard(temp)
                raise
            failed = out.startswith("Error:")
            size = -1
            if not failed:
                try:
                    size = temp.stat().st_size
                except OSError:
                    size = -1
                if size <= 0:
                    failed = True
                    out = (f"Error: `agent-browser {verb}` reported success but wrote "
                           f"{'an empty file' if size == 0 else 'no file'}, so nothing was saved"
                           f"{' and ' + target.name + ' is unchanged' if target.exists() else ''}. "
                           f"Check the page with browser_snapshot (or browser_open a URL), then retry.")
            if failed:
                storage.discard(temp)
                return out
            try:
                storage.commit(temp, target)
            except OSError as e:
                storage.discard(temp)
                return f"Error: could not save {target.name}: {e}"
        await asyncio.to_thread(storage.prune_captures, keep=target)
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
        Use sparingly — prefer snapshot + the action tools. An expression that starts with
        a dash and a letter (`-a`) must be wrapped, `(-a)`, or the CLI reads it as an
        option; `-1` is fine."""
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
        refused.

        The PDF is always US Letter (8.5 x 11 in). The CLI has no paper-size option and
        IGNORES a page's CSS `@page size` — an A4 page comes out Letter. Don't promise the
        user A4: lay the page out for Letter, or say the output is Letter."""
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
