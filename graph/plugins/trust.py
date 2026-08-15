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


def normalize_source(url: str) -> str:
    """A git URL in host/path form — the same rule as the install allowlist's
    ``_source_allowed`` (scheme/``git@`` strip, ``:`` → ``/``), plus a trailing-slash
    and ``.git`` trim, so an acked/official glob matches every spelling of one source.
    Scheme AND ``git@`` strip together (``ssh://git@host/...`` carries both — the
    2733 review caught the single-alternation version leaving ``git@`` behind)."""
    norm = re.sub(r"^(?:(?:https?|git|ssh)://)?(?:git@)?", "", str(url or "")).replace(":", "/").strip()
    norm = norm.rstrip("/")
    return norm[:-4] if norm.endswith(".git") else norm


def _matches(url: str, globs: list[str] | None) -> bool:
    # The prefix fallback is PATH-BOUNDARY widening ("pat/*"), never bare "pat*":
    # an acked exact repo `github.com/x/y` must not silently trust the
    # name-collision `github.com/x/y-evil` (the 2733 review's fix-first finding —
    # a bare-`*` widening is a consent bypass). The boundary form still gives the
    # org shorthand: an entry `github.com/org` matches `github.com/org/repo`.
    #
    # BOTH sides run the one normalizer: an entry hand-written in a canonical
    # spelling (`…/thing.git`, a trailing slash, even a git@ prefix) must keep
    # matching the same repo — trimming only the URL side silently fail-closed
    # `.git`-spelled entries (the 2739 re-review's major). normalize_source is
    # glob-safe: it never touches `*`.
    norm = normalize_source(url)
    pats = [normalize_source(p) for p in globs or []]
    return any(fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(norm, pat + "/*") for pat in pats if pat)


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
