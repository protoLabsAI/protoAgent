"""Safe-command auto-approve allowlist for ``run_command`` (``filesystem.run_auto_approve``).

An operator lists command PREFIXES in argv terms (``git status``, ``npx vitest run``,
``mise exec -- npm test``). A ``run_command`` call that would otherwise pause for HITL
approval runs without it ONLY when every check below passes; anything else — including
anything this module can't classify — falls through to the normal approval prompt. Every
rule here fails CLOSED: a false "no match" costs one extra approval, a false "match" runs
a command nobody looked at.

1. **Character floor.** The raw command contains none of the shell metacharacters or
   expansion triggers in ``_FORBIDDEN_CHARS`` and no control / format / non-ASCII space
   characters. This is checked on the raw string, so a metacharacter inside quotes
   (``git log --format='%H;x'``) is still refused — simplest, and it keeps the rule
   independent of how any shell would parse the quotes.
2. **Tokenising.** ``shlex.split`` (POSIX mode) must succeed.
3. **Token-wise prefix.** The token list starts with an entry's tokens EXACTLY —
   ``git diff`` matches ``git diff --stat`` but not ``git difftool`` / ``git diff-tree``.
4. **Argument denylist.** No token is a known write-anywhere / run-a-program option
   (``--output``, ``-o``, ``--exec``, ``--config``, find's ``-exec``/``-delete`` …). This
   is a cheap best-effort net for read-mostly tools, NOT a sandbox (see the docs).
5. **POSIX only.** Auto-approval applies only when the command would run under ``/bin/sh``
   (``shell`` = default/sh on a non-Windows host); cmd.exe / PowerShell grammars always ask.

A matched command is then executed **directly from those tokens** (``exec``, no shell),
so what was matched is exactly what runs — the character floor is defense in depth, not
the only barrier between the matcher and a shell parser.

Entries are validated the same way when the tools are built (config load / hot reload):
an entry that could never be safely matched, or that is so broad it approves an arbitrary
program (``sh``, ``env``, ``npx``, ``mise exec --``, bare ``git``/``npm``), is dropped with
a warning.
"""

from __future__ import annotations

import logging
import shlex
import unicodedata
from dataclasses import dataclass

log = logging.getLogger("protoagent.fs")

# Shell metacharacters and expansion triggers. With the command exec'd from its shlex
# tokens none of these would reach a shell, but refusing them keeps the transcript
# honest (the operator never sees `a; b` "auto-approved") and fails closed if the exec
# path ever changes. Per character:
#   ; & |          command separators / pipes / background
#   ` $ ( )        command + parameter substitution, subshells
#   < >            redirections (write anywhere)
#   \              escapes — shlex and sh disagree at the edges; not worth reasoning about
#   * ? [ ]        globs — an agent that can write files could plant `--output=x` as a
#                  file name and have `git diff *` expand it into a flag
#   { }            brace expansion (bash-as-sh)
#   ~              tilde expansion (a path outside the project the matcher never sees)
#   #              comment — sh would drop what the matcher counted as arguments
#   !              pipeline negation / history in some shells
# Quotes (' ") are ALLOWED: they only group words, and shlex handles them.
_FORBIDDEN_CHARS = frozenset(";&|`$()<>\\*?[]{}~#!")

# Options that write to an arbitrary path or run an arbitrary program, for the tools
# people typically allowlist (git, find, npm/npx runners, test runners). Matched per
# token, deliberately over-broad (over-matching only costs an approval prompt):
#   * a long option is refused when its name is the denied name OR ANY ABBREVIATION of it
#     — git's parse-options (and npm's nopt) accept unique prefixes, so `--out=/x` IS
#     `--output=/x`;
#   * a single-dash token is refused when a denied short letter appears ANYWHERE in it —
#     short options bundle (`git grep -iOcmd` is `-i -O cmd`), and an attached value
#     (`-ofile`) is covered by the same rule.
_DENY_LONG = frozenset(
    {
        "--output",  # git diff/log/format-patch --output=<file>
        "--output-directory",  # git format-patch
        "--exec",  # git rebase --exec, various
        "--upload-pack",  # git fetch/ls-remote — runs a program
        "--receive-pack",
        "--open-files-in-pager",  # git grep -O<cmd>
        "--ext-diff",  # git diff — runs diff.external
        "--config",  # vitest/jest/tsc-likes: load an arbitrary config (= code)
        "--config-env",
        "--require",  # node-likes: preload a module
        "--import",
        "--loader",
        "--eval",
        "--prefix",  # npm: operate on another directory
        "--userconfig",  # npm: an .npmrc can set script-shell
        "--globalconfig",
        "--script-shell",  # npm: the shell that runs `npm test`'s script
        "--node-options",  # npm: NODE_OPTIONS for the script (--require …)
    }
)
_DENY_SHORT_LETTERS = frozenset("oOcer")
# find(1)-style single-dash long predicates (exact token match).
_DENY_EXACT = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"})

# Entries so broad they approve an arbitrary program: the launcher itself, or a
# launcher + its "run anything" verb. Compared on the entry's FULL token tuple, so
# `npx vitest run` / `mise exec -- npm test` stay legal while `npx` / `mise exec --` don't.
_LAUNCHERS = frozenset(
    {
        "sh", "bash", "zsh", "dash", "ksh", "fish", "env", "xargs", "sudo", "doas", "su",
        "nohup", "nice", "timeout", "time", "command", "exec", "eval", "builtin", "watch",
        "python", "python3", "node", "deno", "bun", "ruby", "perl", "php",
        "npx", "bunx", "pnpx", "uvx", "pipx",
        # multi-verb tools: the bare name reaches `-c`, aliases, `exec`, … — list a subcommand.
        "git", "npm", "pnpm", "yarn", "uv", "mise", "cargo", "go", "make", "poetry", "bundle", "gh",
    }
)  # fmt: skip
_BROAD_PREFIXES = frozenset(
    {
        ("mise", "exec"), ("mise", "exec", "--"), ("mise", "x"), ("mise", "x", "--"), ("mise", "run"),
        ("npm", "exec"), ("npm", "x"), ("npm", "run"), ("npm", "run-script"),
        ("pnpm", "exec"), ("pnpm", "dlx"), ("pnpm", "run"),
        ("yarn", "exec"), ("yarn", "dlx"), ("yarn", "run"),
        ("bun", "run"), ("bun", "x"), ("uv", "run"), ("uv", "tool", "run"), ("poetry", "run"),
        ("bundle", "exec"), ("cargo", "run"), ("go", "run"), ("gh", "api"), ("gh", "alias"),
    }
)  # fmt: skip


def _bad_char(text: str) -> str | None:
    """The first character that disqualifies ``text`` from auto-approval, else None."""
    for ch in text:
        if ch in _FORBIDDEN_CHARS:
            return ch
        if ch == " ":
            continue
        cat = unicodedata.category(ch)
        # C* = control (incl. \n \r \t), format (zero-width, bidi overrides), private,
        # surrogate, unassigned. Z* other than the ASCII space = NBSP, line/para separators.
        if cat[0] in ("C", "Z"):
            return ch
    return None


def _denied_token(tok: str) -> bool:
    if tok in _DENY_EXACT:
        return True
    if tok.startswith("--"):
        name = tok.split("=", 1)[0]
        # `--` alone (end of options) is fine; `--o`, `--out`, `--output` are all `--output`.
        return len(name) > 2 and any(d.startswith(name) for d in _DENY_LONG)
    if tok.startswith("-") and len(tok) > 1:
        return not _DENY_SHORT_LETTERS.isdisjoint(tok[1:])
    return False


def _tokens(text: str) -> list[str] | None:
    """shlex tokens of ``text`` if it passes the character floor and parses, else None."""
    if _bad_char(text) is not None:
        return None
    try:
        return shlex.split(text)
    except ValueError:  # unbalanced quotes
        return None


@dataclass(frozen=True)
class AutoApproveRule:
    entry: str  # the operator's text, normalised to single spaces — shown in logs/marker
    tokens: tuple[str, ...]


def compile_auto_approve(entries) -> list[AutoApproveRule]:
    """Validate ``filesystem.run_auto_approve``; drop (and warn about) unusable entries."""
    if not entries:
        return []
    if isinstance(entries, str):
        entries = [entries]
    rules: list[AutoApproveRule] = []
    for raw in entries:
        reason = None
        toks: list[str] | None = None
        if not isinstance(raw, str) or not raw.strip():
            reason = "not a non-empty string"
        elif (ch := _bad_char(raw.strip())) is not None:
            reason = f"contains a disallowed character {ch!r}"
        else:
            toks = _tokens(raw.strip())
            if not toks:
                reason = "can't be tokenised (unbalanced quotes?)"
            elif "=" in toks[0] or toks[0].startswith("-"):
                reason = "must start with a program name (no VAR=… prefix, no leading option)"
            elif (len(toks) == 1 and toks[0] in _LAUNCHERS) or tuple(toks) in _BROAD_PREFIXES:
                reason = "is too broad — it would approve an arbitrary program; list a specific subcommand"
            elif any(_denied_token(t) for t in toks):
                reason = "contains a denylisted option (writes/executes arbitrary targets)"
        if reason:
            log.warning("[fs] filesystem.run_auto_approve: dropping entry %r — %s", raw, reason)
            continue
        assert toks is not None
        rules.append(AutoApproveRule(entry=" ".join(toks), tokens=tuple(toks)))
    return rules


def match_auto_approve(command: str, rules: list[AutoApproveRule]) -> tuple[AutoApproveRule, list[str]] | None:
    """The first rule that auto-approves ``command`` plus the argv to exec, else None.

    Leading/trailing ASCII spaces are tolerated (shlex ignores them); everything else
    about the string must pass the character floor.
    """
    if not rules:
        return None
    toks = _tokens(command)
    if not toks:
        return None
    if any(_denied_token(t) for t in toks):
        return None
    for rule in rules:
        n = len(rule.tokens)
        if len(toks) >= n and tuple(toks[:n]) == rule.tokens:
            return rule, toks
    return None
