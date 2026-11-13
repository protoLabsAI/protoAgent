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


def has_dot_segment(norm: str) -> bool:
    """A ``.``/``..``/empty path segment (raw or percent-encoded), or an encoded
    ``/``. ``fnmatch``'s ``*`` crosses ``/`` and ``..``, so ``…/protoLabsAI/../evil``
    would match ``…/protoLabsAI/*`` while naming another owner's repo. A leading
    ``/`` (a local absolute path, ``file:///…`` included) is not an empty segment."""
    from urllib.parse import unquote

    path = norm[len("file//") :] if norm.startswith("file//") else norm
    for seg in path.lstrip("/").split("/"):
        dec = unquote(seg)
        if dec == "" or "/" in dec or {".", ".."} & set(dec.split("\\")):
            return True
    return False


# A Windows drive path, bare (``C:/src/x``, ``C:\\src\\x``) or as ``file:///C:/…``. Normalizing
# turns its drive colon into a ``/`` — an "empty segment" that is no traversal at all.
_WINDOWS_LOCAL = re.compile(r"^(?:file:///)?[A-Za-z]:[\\/]")


def source_has_dot_segment(url: str) -> bool:
    """``has_dot_segment`` for a source as the operator wrote it. A Windows drive path is
    judged on its own segments (``.`` / ``..`` only), since its drive colon would otherwise
    read as an empty segment and every such local source would match nothing."""
    raw = str(url or "").strip()
    m = _WINDOWS_LOCAL.match(raw)
    if m:
        from urllib.parse import unquote

        return bool({".", ".."} & set(re.split(r"[\\/]", unquote(raw[m.end() :]))))
    return has_dot_segment(normalize_source(raw))


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
    segment matches nothing (``has_dot_segment``)."""
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
