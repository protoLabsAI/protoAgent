"""Which OAuth-subscription account an ACP delegate burns (ADR 0122 D7).

An ACP coder runs under a CLI login, so the usage it consumes belongs to that login's
provider account — ``claude-agent-acp`` spends the ``anthropic-oauth`` subscription,
``codex-acp`` the ``openai-codex`` one. A delegate may name the account explicitly
(``account:`` on its entry) when it runs under a non-default login; otherwise it is
inferred from the launch incantation (the package in the delegate's ``command``/``args``;
see :mod:`runtime.acp_agents`).

Everything here is read tolerantly: a missing, malformed, or non-ACP delegate maps to
``None``, never an exception — the ADR 0122 D4 invariant that an unknown route behaves
exactly as today.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Package-name → provider-id. A substring match against the delegate's command/args: the
# launch form varies (`npx -y @agentclientprotocol/codex-acp`, a bare `claude-agent-acp`
# binary, or the `claude-code`/`claude-acp` aliases the ACP adapter rewrites to
# claude-agent-acp), but the package token is stable. Codex is listed first so its distinct
# token is checked before the Claude family; both Claude aliases fold to one provider id, so
# a match set of size one is unambiguous.
_PACKAGE_ACCOUNTS: tuple[tuple[str, str], ...] = (
    ("codex-acp", "openai-codex"),
    ("claude-agent-acp", "anthropic-oauth"),
    ("claude-code", "anthropic-oauth"),
    ("claude-acp", "anthropic-oauth"),
)


def account_for_delegate(name_or_entry: Any) -> str | None:
    """The provider id whose subscription delegate ``name_or_entry`` burns, or ``None``.

    ``name_or_entry`` is either a delegate *entry* (a mapping, as it appears in the
    configured ``delegates:`` list) or a delegate *name* (a string), which is resolved
    against the effective roster — this instance's ``langgraph-config.yaml`` entries unioned
    with the fleet-shared host delegates (:func:`graph.config_io.read_host_delegates`), an
    instance entry shadowing a host one of the same name.

    Resolution order (ADR 0122 D7): an explicit ``account:`` on the entry wins; otherwise the
    account is inferred from the launch incantation — ``claude-agent-acp`` →
    ``anthropic-oauth``, ``codex-acp`` → ``openai-codex`` — matching the package name in the
    ``command`` or any of the ``args`` (so ``npx -y @agentclientprotocol/codex-acp`` resolves
    as well as a bare binary). Anything else — a non-ACP delegate, an unknown name, a
    malformed entry — is ``None``. Never raises.
    """
    try:
        entry = _resolve_entry(name_or_entry)
        if entry is None:
            return None
        explicit = entry.get("account")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        return _infer_account(entry)
    except Exception:  # noqa: BLE001 — an unknown route degrades to None, never a raise (D4)
        log.debug("[usage] account_for_delegate failed", exc_info=True)
        return None


def _resolve_entry(name_or_entry: Any) -> dict | None:
    """A delegate entry from either the entry itself or a name lookup; ``None`` if neither."""
    if isinstance(name_or_entry, dict):
        return name_or_entry
    if isinstance(name_or_entry, str) and name_or_entry.strip():
        name = name_or_entry.strip()
        for entry in _configured_delegates():
            if isinstance(entry, dict) and str(entry.get("name") or "") == name:
                return entry
    return None


def _infer_account(entry: dict) -> str | None:
    """Infer the account from the delegate's launch command/args, or ``None``."""
    parts: list[Any] = [entry.get("command")]
    args = entry.get("args")
    if isinstance(args, (list, tuple)):
        parts.extend(args)
    haystack = " ".join(p for p in parts if isinstance(p, str)).lower()
    matched = {account for token, account in _PACKAGE_ACCOUNTS if token in haystack}
    # Exactly one provider → unambiguous (the Claude aliases collapse to one id). A mixed or
    # empty entry is not something we route — leave it to default behaviour.
    return next(iter(matched)) if len(matched) == 1 else None


def _configured_delegates() -> list[dict]:
    """The effective delegate roster (instance ∪ fleet-shared), read tolerantly. Imported
    lazily so this leaf module stays cheap to import and free of config side effects until a
    name lookup actually needs them."""
    from graph.config_io import load_yaml_doc, read_host_delegates

    agent: list[dict] = []
    try:
        doc = load_yaml_doc() or {}
        val = doc.get("delegates") if isinstance(doc, dict) else None
        if isinstance(val, list):
            agent = [e for e in val if isinstance(e, dict)]
    except Exception:  # noqa: BLE001 — an unreadable config contributes no delegates
        agent = []
    try:
        host = read_host_delegates()
    except Exception:  # noqa: BLE001 — same tolerance read_host_delegates documents
        host = []
    names = {str(e.get("name") or "") for e in agent}
    return agent + [e for e in host if isinstance(e, dict) and str(e.get("name") or "") not in names]
