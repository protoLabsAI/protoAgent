"""Install-time trust resolution (ADR 0071 D3 — S1/S2, #2721).

Three config knobs decide whether installing from a source needs a one-time
"this runs code" ack:

* ``plugins.sources.official`` — source globs auto-TRUSTED at install. Default =
  the protoLabsAI org; **fork-overridable** so a fork points auto-trust at its own
  org by config, never a core edit (the operator-fork contract). An EXPLICIT empty
  list means "no official sources" — a valid hardening choice, distinct from the
  key being absent.
* ``plugins.sources.acked`` — sources the operator has confirmed once (written by
  the consent ack; stored as the exact normalized source so one confirm covers
  every spelling of that repo, and the operator can hand-widen entries to globs).
* ``plugins.trust_unverified`` — the "don't ask again" switch: every source is
  treated as acked.

Official/acked/trusted decides ONLY whether the consent ask is required before an
install runs code. It changes nothing else: install ≠ enable ≠ trust semantics
(ADR 0027) are untouched, and manifest ``capabilities:`` stay disclosure, not
enforcement (ADR 0071 — the runtime has no sandbox to enforce with). The ask
itself is wired by the ack API + console dialog (S4/S6).
"""

from __future__ import annotations

import fnmatch
import re


def _strip_source_prefix(value: str) -> str:
    """The shared half of both normalizers: scheme AND ``git@`` strip together
    (``ssh://git@host/...`` carries both — the 2733 review caught the
    single-alternation version leaving ``git@`` behind), ``:`` → ``/``, and the
    trailing-slash trim. The ``.git`` handling deliberately stays with each caller:
    unconditional for URLs, glob-aware for pattern entries."""
    norm = re.sub(r"^(?:(?:https?|git|ssh)://)?(?:git@)?", "", str(value or "")).replace(":", "/").strip()
    return norm.rstrip("/")


def normalize_source(url: str) -> str:
    """A git URL in host/path form — prefix strip (see ``_strip_source_prefix``)
    plus an unconditional ``.git`` trim, so an acked/official entry matches every
    spelling of one source."""
    norm = _strip_source_prefix(url)
    return norm[:-4] if norm.endswith(".git") else norm


_GLOB_CHARS = frozenset("*?[")


def _normalize_pattern(pat: str) -> str:
    """A pattern entry in the same host/path form the URL side normalizes to.

    Scheme/``git@`` strip, ``:`` → ``/``, and the trailing-slash trim are safe for
    every entry. The ``.git`` trim applies ONLY to glob-free (exact-repo) entries:
    on a glob it CHANGES semantics fail-open — ``github.com/acme/*.git`` would
    become ``…/acme/*`` (admitting the whole org) and a bare ``*.git`` would
    become ``*`` (admitting everything) — the 2739 round-3 finding."""
    norm = _strip_source_prefix(pat)
    if norm.endswith(".git") and not _GLOB_CHARS.intersection(norm):
        norm = norm[:-4]
    return norm


def _source_path(raw: str) -> str:
    """The PATH part of a source as written — what names the repo — with the host, port and
    any userinfo left out. Read from the raw string, not the colon-normalized one: there an
    scp-style ``git@host:/srv/x``, a port or a bracketed IPv6 host each leave a ``//`` that
    is no part of any path."""
    scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", raw)
    if scheme:
        rest = raw[scheme.end() :]
        if scheme.group(1).lower() == "file":
            return rest
        _authority, sep, path = rest.partition("/")
        return sep + path
    if raw.startswith("/"):
        return raw
    head = raw.split("/", 1)[0]
    if ":" in head:  # scp-style ``[user@]host:path``
        return raw.split(":", 1)[1]
    _host, sep, path = raw.partition("/")
    return sep + path


# A Windows drive path, bare (``C:/src/x``, ``C:\\src\\x``) or as ``file:///C:/…``: its drive
# colon is not a host separator.
_WINDOWS_LOCAL = re.compile(r"^(?:file:///)?[A-Za-z]:[\\/]")


def source_has_dot_segment(url: str) -> bool:
    """Whether a source's path has a ``.`` / ``..`` / empty segment (raw or percent-encoded)
    or an encoded ``/``. ``fnmatch``'s ``*`` crosses ``/`` and ``..``, so
    ``…/protoLabsAI/../evil`` would match ``…/protoLabsAI/*`` while naming another owner's
    repo; such a source matches nothing and is refused at install. One leading and one
    trailing ``/`` are the path's own edges, not empty segments."""
    from urllib.parse import unquote

    raw = str(url or "").strip()
    win = _WINDOWS_LOCAL.match(raw)
    path = raw[win.end() :] if win else _source_path(raw)
    if path.startswith("/"):
        path = path[1:]
    if path.endswith("/"):
        path = path[:-1]
    if not path:
        return False
    for seg in path.split("/"):
        dec = unquote(seg)
        if dec == "" or "/" in dec or {".", ".."} & set(dec.split("\\")):
            return True
    return False


def source_matches(url: str, globs: list[str] | None) -> bool:
    """THE match predicate — shared by the trust matcher and the installer
    allowlist so the two can never drift (they did, byte-for-byte, twice).

    The prefix fallback is PATH-BOUNDARY widening (``pat/*``), never bare
    ``pat*``: an exact entry ``github.com/x/y`` must not match the
    name-collision ``github.com/x/y-evil`` (a bare-``*`` widening is a consent
    bypass). The boundary form still gives the org shorthand: an entry
    ``github.com/org`` matches ``github.com/org/repo``. Both sides normalize —
    exact entries fully (spelling-insensitive), glob entries without the
    ``.git`` trim (see ``_normalize_pattern``). A source with a dot/empty path
    segment matches nothing (``source_has_dot_segment``)."""
    norm = normalize_source(url)
    if source_has_dot_segment(url):
        return False
    pats = [_normalize_pattern(p) for p in globs or []]
    return any(fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(norm, pat + "/*") for pat in pats if pat)


def _matches(url: str, globs: list[str] | None) -> bool:
    return source_matches(url, globs)


def source_official(url: str, official: list[str] | None) -> bool:
    """Does ``url`` fall under an official (auto-trusted) source glob?"""
    return _matches(url, official)


def source_trusted(
    url: str,
    *,
    official: list[str] | None,
    acked: list[str] | None,
    trust_unverified: bool = False,
) -> bool:
    """True when installing from ``url`` needs NO consent ack: an official source,
    one the operator already acked, or the global don't-ask switch."""
    return bool(trust_unverified) or _matches(url, official) or _matches(url, acked)


def ack_pattern(url: str) -> str:
    """What a bare Confirm stores into ``plugins.sources.acked``: the exact
    normalized source (narrowest grant — acking one repo trusts that repo, not its
    whole org; official is where org-wide trust lives)."""
    return normalize_source(url)
