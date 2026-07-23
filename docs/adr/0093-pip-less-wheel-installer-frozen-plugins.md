# 0093 — Pip-less wheel installer for frozen-app plugin deps

Status: **Accepted** (phased — P1 pure wheels shipped #2151; P2 platform wheels deferred #2155)

## Context

[ADR 0058](./0058-runtime-plugin-install-frozen-app.md) D2 gates plugin installs in the
frozen desktop sidecar: a plugin whose `requires_pip` entries aren't already importable in
the read-only PyInstaller bundle is refused with *"install it on a server/Docker build
instead."* [ADR 0092](./0092-desktop-document-baseline-and-versioned-file-artifacts.md) bakes
the **first-party document-generation stack** into the bundle so cowork works — but that's a
curated baseline. **Any third-party plugin that declares an unbundled dep is still refused**,
even when the operator would happily accept the risk. That's the open half of
[#1631](https://github.com/protoLabsAI/protoAgent/issues/1631) (Scope A).

The refusal isn't just policy — three facts constrain it:

- **No pip.** The non-frozen path shells out to `[sys.executable, "-m", "pip", ...]`, but in a
  PyInstaller app `sys.executable` **is the app binary**, not a Python with pip.
- **The bundle is read-only** — you cannot install into the frozen site-packages.
- ADR 0058 explicitly **rejected shipping git + pip** in the desktop bundle.

But the seam already reaches the frozen app. `loader._plugin_roots()` returns
`[bundle/plugins, live_root]` in **all** modes; a plugin dropped into the writable
`live_root` (`ip.plugins_dir`) is discovered and loaded **by file path**
(`spec_from_file_location` + `exec_module`), which works in a frozen binary. ADR 0058 D1
already solved the **git** gap with a git-less HTTPS archive fetch. **The only remaining
frozen gap is `pip`** — and that's what this ADR closes, the same way D1 closed git: replace
the subprocess with an in-process, httpx-based equivalent, behind an explicit opt-in.

## Decision

### D1 — Opt-in only, never default-on

A new `plugins.allow_unbundled_deps: true` (off by default) plus the existing one-time
*"this installs code that runs on import"* confirm. Installing packages is code-exec-adjacent,
so it stays consent-gated — consistent with [ADR 0071](./0071-plugin-permissions-trust-model.md)
(permissions = trust-and-consent, not a sandbox). With the flag off, ADR 0058 D2's refusal
stands unchanged.

### D2 — A writable per-instance deps dir on `sys.path`

Wheels unpack into `<box_root>/plugin-deps/<plugin_id>/`, prepended to `sys.path` at boot in
frozen mode — the same pattern that already makes `live_root` work when frozen. Per-plugin
so `uninstall --purge` removes exactly that plugin's dir; per-instance so instances don't
share a mutable dep set.

### D3 — Wheel fetch over httpx (reuse the D1 archive-fetch pattern)

Resolve a package via **PyPI's JSON API** (`/pypi/<name>/json`, `/pypi/<name>/<version>/json`),
download the chosen **wheel**, and unpack it (a wheel is a zip) into the deps dir. **Wheels
only** — no sdists: a source dist needs a compiler + build backend the bundle doesn't have, so
an sdist-only package keeps today's refusal with a clear message.

### D4 — P1 pure wheels, then P2 platform wheels *(the phasing #1631 asked for)*

- **P1 — pure-Python wheels** (`py3-none-any`): no tag matching needed; unpack and go. Covers
  the majority of plugin deps and ships first.
- **P2 — platform wheels**: match the wheel's compatibility tag against the **bundled runtime**
  via `packaging.tags.sys_tags()`. A matching wheel (right CPython ABI + platform) installs; a
  wheel with no compatible tag keeps the refusal (with the reason). Covers the
  `lxml`/`Pillow`/`reportlab`-class deps — note those specific ones are already bundled by
  ADR 0092, so P2's value is *other* third-party platform wheels.

### D5 — Transitive resolution without pip

PyPI's JSON hands you `requires_dist` per package, so this is a small resolver:

- Evaluate **env markers** (`packaging.markers`) against the frozen runtime — drop deps whose
  marker is false (e.g. `; python_version < "3.11"`).
- Honor **version specifiers** (`packaging.specifiers`) — pick the highest compatible version.
- **Short-circuit** any dep already importable in the bundle (`_deps_satisfied`) — never
  re-fetch what ADR 0092 or the core already ship.
- Bounded depth + a visited-set cycle guard; a package that can't be resolved (sdist-only, no
  compatible wheel, unsatisfiable specifier) fails the whole install with the offending name.

### D6 — Keep the ADR 0058 rails + pin

`_validate_pip_specs` still rejects pip options / VCS / URL / direct references. `_deps_satisfied`
still short-circuits when the bundle covers everything (no network at all). Installs are
**audit-logged** the way `install_deps` already does. Each resolved `(name, version, wheel
sha256)` is recorded in `plugins.lock` so a re-install is **reproducible and tamper-evident**
(a mismatched hash aborts).

## Consequences

- **Supersedes ADR 0058 D2's "refuse unbundled deps" for the opt-in case.** The refusal remains
  the default (flag off), and for sdist-only packages and unmatched platform wheels.
- **Complements ADR 0092.** 0092 *bakes* the flagship stack into the bundle (always present, no
  opt-in); 0093 *installs* arbitrary third-party deps at runtime behind consent. A dep already
  in the bundle short-circuits 0093 entirely.
- **Security:** package code runs on import → opt-in + confirm + hash-pin + the spec rails. Not a
  sandbox (ADR 0071). The threat model is unchanged from installing any plugin's code today; the
  new surface is *transitive* deps, which the lock's hashes pin.
- **Verification gap (#1631):** real confidence needs a PyInstaller build + a manual install — CI
  can only simulate the frozen path via `PROTOAGENT_PLUGIN_FROZEN=1`, and the deps-dir `sys.path`
  prepend touches frozen-sidecar boot, so a boot test must cover it.
- **Phasing:** P1 (pure wheels) is the first implementation PR; P2 (platform-wheel tag matching)
  the second. sdist-only stays refused — building is explicitly out of scope.

## References

- [ADR 0058](./0058-runtime-plugin-install-frozen-app.md) — the D2 gate this opens (and its D1 git-less fetch, the pattern reused here)
- [ADR 0092](./0092-desktop-document-baseline-and-versioned-file-artifacts.md) — the complementary first-party bundle baseline
- [ADR 0071](./0071-plugin-permissions-trust-model.md) — trust-and-consent posture the opt-in follows
- ADR 0027 D4 — git-URL plugin install (the `install_deps` path this mirrors for frozen)
- [#1631](https://github.com/protoLabsAI/protoAgent/issues/1631) Scope A — the tracking issue, LOE + P1/P2 split
- `graph/plugins/installer.py` (`_frozen_like`, `_deps_satisfied`, `_validate_pip_specs`, the D2 gate), `graph/plugins/loader.py` (`_plugin_roots`)
