"""Agent snapshot export — the declarative recipe, secret-free (ADR 0091 D1/D2, #2103).

Emits one agent's **definition** as a zip: SOUL, secret-stripped config, pinned plugin
set, subagents, MCP servers, skills. Not a state dump — no sqlite, no history, no
credentials. ADR 0091 rejected the raw instance-root zip as the ``docker commit`` /
committed-``.tfstate`` anti-pattern: heavy, version-brittle, and unauditably secret-laden.

**The acceptance bar is the 12-Factor litmus: this artifact could be pushed to a public
gist without leaking a single credential.** Everything below follows from taking that
literally.

Two independent redaction layers, because they fail differently:

1. **Structural** — every ``(section, key)`` in ``config_io.secret_paths()`` is removed
   outright, plus ``mcp.servers[].env`` / ``.headers``, which live inline in the main
   config and are NOT covered by ``SECRET_PATHS`` (ADR 0091 names this the sharpest leak
   risk). Catches known, declared credentials exactly.
2. **Pattern** — ``export_op.redact`` swept over every free-text value that ships. A
   vendor-shaped token pasted into a plugin's text field, an MCP arg, or SOUL.md is not
   key-shaped, so layer 1 cannot see it. Conservative about false negatives by design.

**This module deliberately does NOT use ``config_io.strip_secrets_from_doc``.** That
function is the *save* path and fails OPEN on purpose: if relocating a value into
``secrets.yaml`` fails, it leaves the secret inline rather than lose the operator's
credential (#1645). That is right for a file staying on the box and catastrophic for an
artifact meant to leave it. It also *writes* to ``secrets.yaml`` as a side effect, which
an export must never do. Export fails CLOSED: a value we cannot prove is safe does not
ship, even at the cost of dropping it — the config is still on the source machine.

What the target must re-provide travels as ``required_secrets``: names and descriptions,
never values (Letta Agent File's null-on-export, the A2A card's ``securitySchemes``). So
import can prompt instead of silently producing a broken agent.

Host-free and unit-testable: every path is an argument, nothing imports ``server`` or
reads ``STATE`` — the ``export_op`` / ``rewind_op`` shape.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SNAPSHOT_MANIFEST = "agent.snapshot.yaml"
SNAPSHOT_VERSION = 1

#: Domains never exported as a knowledge seed, whatever the operator asks for.
#:
#: ``hot`` and the session-summary domains are the agent's MEMORY (ADR 0069) — what it
#: recalls about a specific person's sessions, not knowledge about a subject. That is a
#: different kind of data with a different consent question, and a snapshot is a thing you
#: hand to someone else. #2105 left "seed memory too" open; the answer here is no. Knowledge
#: can be reviewed domain by domain before it travels; memory is accreted, personal, and
#: nobody would enjoy auditing it line by line under time pressure.
MEMORY_DOMAINS = frozenset({"hot", "session", "conversation", "finding"})

#: Files that must never enter a snapshot, matched by name anywhere in the tree. The
#: structural strip handles config VALUES; these are whole files that exist only to hold
#: credentials or machine identity, so no amount of redaction makes them portable.
#: ADR 0091 D2 names the first three explicitly.
EXCLUDED_FILENAMES = frozenset(
    {
        "secrets.yaml",  # the credential overlay itself
        "devices.json",  # hashed device tokens (ADR 0087 pairing)
        ".fleet-token",  # the fleet service token (ADR 0089)
        ".instance-uid",  # this box's identity for this instance — never a target's
    }
)

#: Config sections dropped wholesale: machine-local or identity-bearing, meaningless (or
#: actively wrong) on a different box. Kept narrow — a snapshot's value is that it carries
#: the operator's real configuration, so anything not justified here travels.
#: Pattern kinds that are NOT credentials — they are machine-local identity that must not
#: travel, but there is nothing to rotate. Split out so the review can tell an operator the
#: right thing to do: a scrubbed token means "go rotate that", a scrubbed home path means
#: "the target will need to re-point this". Conflating them sends people hunting for a
#: breach that never happened.
NON_CREDENTIAL_KINDS = frozenset({"home-path"})

EXCLUDED_SECTIONS = frozenset(
    {
        "secrets_manager",  # ADR 0080 bootstrap identity; its keys are secret_paths anyway
    }
)


@dataclass
class SecretRequirement:
    """One credential the target must supply before the rehydrated agent works."""

    name: str  # dotted config path ("model.api_key") or "mcp.<server>.env.<VAR>"
    kind: str  # "config" | "mcp_env" | "mcp_header" | "plugin"
    description: str = ""
    #: True when the source agent actually had a value here. A requirement discovered from
    #: a plugin manifest that the operator never filled in is still worth listing (import
    #: should ask), but the operator should be able to tell the two apart.
    was_set: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "description": self.description, "was_set": self.was_set}


@dataclass
class KnowledgeSeed:
    """Domain-tagged source docs carried by an OPT-IN export (ADR 0091 D4, #2105).

    Deliberately documents, not the raw ``knowledge/agent.db``: sqlite is version-brittle
    and carries embeddings computed against the SOURCE's gateway, which mean nothing on a
    target that may use a different model. Text re-ingests anywhere.
    """

    #: domain → the concatenated markdown for that domain.
    docs: dict[str, str] = field(default_factory=dict)
    #: domain → chunk count, for the review.
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total_chars(self) -> int:
        return sum(len(v) for v in self.docs.values())


@dataclass
class SnapshotResult:
    """The built artifact plus everything an operator needs to review before sharing."""

    data: bytes
    filename: str
    manifest: dict
    required_secrets: list[SecretRequirement] = field(default_factory=list)
    #: Redaction kinds the PATTERN sweep matched, keyed by WHERE — a dotted config path
    #: (``operator.project_dir``, ``notes.scratch[0]``) or a filename (``SOUL.md``). Non-empty
    #: means something was scrubbed from free text and is worth an operator's eyes.
    #:
    #: The keys are deliberately config paths, not opaque labels: import (#2104) can prompt
    #: for exactly these without re-deriving them by scanning the config for redaction
    #: markers. A `home-path` hit is the machine-local value the target must re-point.
    pattern_redactions: dict[str, list[str]] = field(default_factory=dict)
    #: Human-facing notes (skipped files, absent optional pieces).
    notes: list[str] = field(default_factory=list)
    #: domain → chunk count when a knowledge seed was included. Non-empty flips the whole
    #: artifact from "publishable" to "review before sharing" — see ``render_review``.
    knowledge: dict[str, int] = field(default_factory=dict)

    @property
    def carries_knowledge(self) -> bool:
        return bool(self.knowledge)

    def summary(self) -> dict:
        return {
            "filename": self.filename,
            "bytes": len(self.data),
            "required_secrets": [s.as_dict() for s in self.required_secrets],
            "pattern_redactions": self.pattern_redactions,
            "notes": self.notes,
            "knowledge": self.knowledge,
            "carries_knowledge": self.carries_knowledge,
        }


def _redact_text(value: str, where: str, found: dict[str, list[str]]) -> str:
    """Pattern-sweep one free-text value, recording what was hit and where."""
    from graph.export_op import redact

    cleaned, kinds = redact(value)
    if kinds:
        found.setdefault(where, [])
        for k in kinds:
            if k not in found[where]:
                found[where].append(k)
    return cleaned


def _sweep(value: Any, where: str, found: dict[str, list[str]]) -> Any:
    """Recursively pattern-sweep every string in a config subtree.

    Layer 2 of the redaction. Runs over what SURVIVED the structural strip, so it is a net
    for the credential nobody declared — a token in a plugin's free-text field, a
    credentialed URL in an MCP arg. Structure is preserved; only string leaves change.
    """
    if isinstance(value, str):
        return _redact_text(value, where, found)
    if isinstance(value, dict):
        return {k: _sweep(v, f"{where}.{k}" if where else str(k), found) for k, v in value.items()}
    if isinstance(value, list):
        return [_sweep(v, f"{where}[{i}]", found) for i, v in enumerate(value)]
    return value


def _has_value(value: Any) -> bool:
    return bool(value.strip()) if isinstance(value, str) else value is not None and value is not False


def _overlay_has(stored: dict | None, section: str, key: str) -> bool:
    """Whether ``secrets.yaml`` holds a value for ``section.key``. Reads the boolean only —
    the value itself never leaves this function."""
    sect = (stored or {}).get(section)
    return _has_value(sect.get(key)) if isinstance(sect, dict) else False


def redact_config_for_export(
    doc: dict,
    *,
    secret_key_paths: tuple[tuple[str, str], ...],
    stored_secrets: dict | None = None,
) -> tuple[dict, list[SecretRequirement], dict[str, list[str]]]:
    """Return ``(clean_config, required_secrets, pattern_redactions)``.

    ``stored_secrets`` is the ``secrets.yaml`` overlay, read ONLY to decide ``was_set``.
    A correctly-configured agent keeps its credentials there and not inline, so without it
    every properly-stored secret reports "declared, unset" — exactly inverting the signal,
    and telling the operator their configured agent needs nothing. No value from it is ever
    emitted; only the boolean.

    PURE — never mutates ``doc``, never touches ``secrets.yaml``. Deliberately unlike
    ``config_io.strip_secrets_from_doc`` (see module docstring): a key listed as secret is
    removed unconditionally, with no relocation and no leave-it-inline fallback. If we
    cannot prove a value is safe it does not ship.
    """
    import copy

    clean = copy.deepcopy(doc) if isinstance(doc, dict) else {}
    required: list[SecretRequirement] = []

    # ── layer 1a: declared secret keys ──
    # Walk EVERY declared path, not just the ones sitting inline. A correctly-configured
    # agent keeps its credentials in secrets.yaml, so an inline-only walk skips exactly the
    # real ones — `model.api_key` (the gateway key the agent cannot run without) vanished
    # from the inventory entirely, which defeats the point of shipping an inventory.
    for section, key in secret_key_paths:
        sect = clean.get(section)
        inline = isinstance(sect, dict) and key in sect
        in_overlay = _overlay_has(stored_secrets, section, key)
        if not inline and not in_overlay:
            continue  # neither configured nor stored — a plugin manifest may still declare it
        value = sect.pop(key) if inline else None
        was_set = _has_value(value) or in_overlay
        required.append(
            SecretRequirement(
                name=f"{section}.{key}",
                kind="plugin" if section not in ("model", "auth") else "config",
                description=f"Credential for `{section}` (stripped on export).",
                was_set=was_set,
            )
        )
        if isinstance(sect, dict) and not sect:
            clean.pop(section, None)

    for section in EXCLUDED_SECTIONS:
        clean.pop(section, None)

    # ── layer 1b: MCP env / headers — inline in the main config, NOT in SECRET_PATHS ──
    # ADR 0091 D2 calls this the sharpest leak risk: `mcp.servers[].env` holds live API
    # tokens as ordinary config, so the declared-key strip above walks straight past it.
    # KEYS are kept (the target needs to know WHICH vars to set) and values are nulled.
    mcp = clean.get("mcp")
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    if isinstance(servers, list):
        for entry in servers:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "?")
            for field_name, kind in (("env", "mcp_env"), ("headers", "mcp_header")):
                values = entry.get(field_name)
                if not isinstance(values, dict):
                    continue
                for var in list(values):
                    was_set = bool(str(values.get(var) or "").strip())
                    values[var] = ""
                    required.append(
                        SecretRequirement(
                            name=f"mcp.{name}.{field_name}.{var}",
                            kind=kind,
                            description=f"`{var}` for MCP server `{name}` (value nulled on export).",
                            was_set=was_set,
                        )
                    )

    # ── layer 2: pattern sweep over everything that survived ──
    found: dict[str, list[str]] = {}
    clean = _sweep(clean, "", found)
    return clean, required, found


def discover_plugin_requirements() -> list[SecretRequirement]:
    """Credentials each installed plugin DECLARES it needs, read from its manifest.

    Listed even when unset on the source: import should ask for them rather than stand up
    an agent whose plugin silently can't authenticate.

    This is the ONE piece of the exporter that reads global state (the installed-plugin
    registry), so ``build_snapshot`` takes it as an injectable argument rather than calling
    it directly — otherwise a "does a clean agent export cleanly?" test would depend on
    whichever plugins the developer happens to have installed, which is exactly how it was
    caught.
    """
    out: list[SecretRequirement] = []
    try:
        from graph.plugins.pconfig import installed_plugin_config_schemas

        for schema in installed_plugin_config_schemas(strict=False):
            for key in schema.secrets or []:
                out.append(
                    SecretRequirement(
                        name=f"{schema.section}.{key}",
                        kind="plugin",
                        description=f"Declared by the `{schema.section}` plugin manifest.",
                    )
                )
    except Exception:  # noqa: BLE001 — discovery is best-effort; the config strip already covers set values
        log.warning("[snapshot] plugin secret discovery failed — manifest-declared secrets may be missing", exc_info=True)
    return out


def _dedupe(requirements: list[SecretRequirement]) -> list[SecretRequirement]:
    """One row per credential name. A requirement discovered from BOTH a live config value
    and a plugin manifest keeps ``was_set=True`` — the operator needs to know the source
    agent actually had one, so the target can be told it is genuinely required."""
    by_name: dict[str, SecretRequirement] = {}
    for req in requirements:
        prior = by_name.get(req.name)
        if prior is None:
            by_name[req.name] = req
        elif req.was_set and not prior.was_set:
            prior.was_set = True
    return sorted(by_name.values(), key=lambda r: r.name)


def _safe_domain(domain: str) -> str:
    """A filename-safe domain. The domain is agent-authored (a tool call can create one), so
    it must not be able to steer where the member lands inside the zip."""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(domain))
    return cleaned.strip("-") or "general"


def _read_yaml(path: Path) -> dict:
    import yaml

    if not path.exists():
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        log.warning("[snapshot] could not read %s — treating as empty", path)
        return {}
    return doc if isinstance(doc, dict) else {}


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("[snapshot] could not read %s — treating as empty", path)
        return {}
    return doc if isinstance(doc, dict) else {}


def _skill_files(root: Path) -> list[tuple[str, Path]]:
    """``SKILL.md`` dirs travel whole: skills re-seed from disk each boot (ADR 0060), so
    the files ARE the definition. Returns (arcname-relative, path) pairs."""
    if not root.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in EXCLUDED_FILENAMES:
            continue
        out.append((str(path.relative_to(root)), path))
    return out


def render_review(
    *,
    agent_name: str,
    required: list[SecretRequirement],
    pattern_redactions: dict[str, list[str]],
    notes: list[str],
    exported_at: str,
    knowledge: dict[str, int] | None = None,
) -> str:
    """The operator-facing review, written INTO the zip as ``REVIEW.md``.

    It lives inside the artifact rather than only in an API response so it can never be
    separated from what it describes: whoever opens this zip in six months sees what was
    stripped and what the target still has to supply. Mirrors the chat export, which
    discloses its redactions in the document itself for the same reason (#2158) — an
    export is meant to leave the machine, so the operator reviews rather than trusting a
    silent filter.
    """
    knowledge = knowledge or {}
    lines = [
        f"# Snapshot review — {agent_name}",
        "",
        f"Exported {exported_at}.",
        "",
        "This is a **declarative recipe, not a state dump**: no runtime history, no",
        "credentials, no plugin code (plugins are pinned by SHA and re-installed on import).",
        "",
    ]

    # The framing has to change when knowledge rides along, and change LOUDLY.
    #
    # Every other export can be published anywhere — that is the 12-Factor litmus the whole
    # feature is built to. A knowledge seed breaks it, and breaks it along a DIFFERENT axis
    # than credentials: this text contains no secrets and may still be the last thing you
    # want public. An operator who opted in, then read a review that still said "secret-free",
    # would have been told the truth and misled at the same time. So the claim is retracted
    # here, at the top, before the reassuring parts.
    if knowledge:
        total = sum(knowledge.values())
        lines += [
            "> [!WARNING]",
            "> **This snapshot carries a knowledge seed — do NOT treat it as publishable.**",
            f"> {total} chunk(s) of this agent's knowledge travel with it, as text.",
            "> Secret-free is not the same as safe-to-share: none of it is a credential, and",
            "> all of it may be private. The usual \"you could push this to a public gist\"",
            "> property does **not** hold for this file. Share it the way you would share the",
            "> source documents themselves.",
            "",
            "| Domain | Chunks |",
            "| --- | --- |",
            *[f"| `{d}` | {n} |" for d, n in sorted(knowledge.items())],
            "",
            "*The agent's MEMORY is never included — what it recalls about a person's sessions",
            "is a different kind of data with a different consent question.*",
            "",
        ]

    lines += [
        "## Credentials the target must supply",
        "",
    ]
    if required:
        lines.append("| Credential | Kind | Set on source |")
        lines.append("| --- | --- | --- |")
        for r in required:
            lines.append(f"| `{r.name}` | {r.kind} | {'yes' if r.was_set else 'no'} |")
        lines += [
            "",
            "*Names only — no values travel. \"Set on source\" says whether the exporting agent",
            "actually had one, so an unset row is a credential the plugin declares but nobody",
            "filled in.*",
        ]
    else:
        lines.append("*None — this agent had no configured credentials.*")

    creds = {w: [k for k in ks if k not in NON_CREDENTIAL_KINDS] for w, ks in pattern_redactions.items()}
    creds = {w: ks for w, ks in creds.items() if ks}
    local = {w: [k for k in ks if k in NON_CREDENTIAL_KINDS] for w, ks in pattern_redactions.items()}
    local = {w: ks for w, ks in local.items() if ks}

    lines += ["", "## Credential-shaped text found and scrubbed", ""]
    if creds:
        lines += [
            "Something credential-shaped was sitting in **free text**, where the declared-key",
            "strip cannot see it. It is gone from this artifact — but it is still in the SOURCE",
            "agent, so treat it as exposed: rotate it, then remove it there.",
            "",
        ]
        for where, kinds in sorted(creds.items()):
            lines.append(f"- `{where or '(root)'}` — {', '.join(kinds)}")
    else:
        lines.append("*None — nothing credential-shaped was found in free text.*")

    lines += ["", "## Machine-local values the target must re-point", ""]
    if local:
        lines += [
            "Not credentials — nothing to rotate. These are paths tied to the exporting machine,",
            "scrubbed because they carry the operator's username. They would not have resolved on",
            "the target anyway; set them to local paths after import.",
            "",
        ]
        for where, kinds in sorted(local.items()):
            lines.append(f"- `{where or '(root)'}` — {', '.join(kinds)}")
    else:
        lines.append("*None.*")

    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]

    lines += [
        "",
        "## Caveat",
        "",
        "Pattern redaction is a **safety net, not a guarantee** — it is deliberately",
        "conservative about false negatives, but it cannot recognize a credential that looks",
        "like ordinary prose. Read this artifact before publishing it.",
        "",
    ]
    return "\n".join(lines)


def build_snapshot(
    *,
    config_yaml: Path,
    soul_path: Path,
    plugins_lock: Path,
    skills_dirs: dict[str, Path] | None = None,
    agent_name: str = "",
    secret_key_paths: tuple[tuple[str, str], ...] | None = None,
    plugin_requirements: list[SecretRequirement] | None = None,
    secrets_yaml: Path | None = None,
    knowledge: KnowledgeSeed | None = None,
    now: datetime | None = None,
) -> SnapshotResult:
    """Build the snapshot zip in memory. Every input is an explicit path — no ``STATE``,
    no ``instance_paths()`` call — so a test can point it at a fixture tree and the caller
    (route or CLI) owns path resolution.

    ``knowledge`` (#2105) is an OPT-IN seed the caller collects — default ``None`` means the
    snapshot carries the definition only, which is what keeps the ordinary artifact
    publishable. Passing one is a deliberate operator choice and changes what the review says
    about sharing this file.

    ``secret_key_paths`` and ``plugin_requirements`` default to live discovery when the
    caller omits them; pass them explicitly to keep a test off the developer's machine
    state. Pass ``plugin_requirements=[]`` for "this agent has no plugins", NOT ``None``,
    which means "go look".
    """
    if secret_key_paths is None:
        from graph.config_io import secret_paths

        secret_key_paths = secret_paths()
    if plugin_requirements is None:
        plugin_requirements = discover_plugin_requirements()

    notes: list[str] = []
    raw_config = _read_yaml(config_yaml)
    # Default the name from the config being exported rather than asking the host. It is
    # the same source `server.agent_name()` reads (`identity.name`), it stays correct if
    # the caller is exporting some OTHER instance's tree, and it keeps this module free of
    # a `server` import — which `operator_api` callers could not make anyway without adding
    # to the import-contract burndown list.
    if not agent_name:
        identity = raw_config.get("identity")
        agent_name = str((identity or {}).get("name") or "").strip() if isinstance(identity, dict) else ""
        agent_name = agent_name or "agent"
    if not raw_config:
        notes.append(f"no config found at {config_yaml.name} — the snapshot carries an empty config")
    # Read-only, and never packaged: the overlay decides `was_set`, nothing more. The file
    # itself is in EXCLUDED_FILENAMES and can never become a zip member.
    stored_secrets = _read_yaml(secrets_yaml) if secrets_yaml else {}
    clean_config, config_reqs, pattern_hits = redact_config_for_export(
        raw_config, secret_key_paths=secret_key_paths, stored_secrets=stored_secrets
    )

    soul_text = ""
    if soul_path.exists():
        try:
            soul_text = soul_path.read_text(encoding="utf-8")
        except OSError:
            notes.append(f"could not read {soul_path.name}")
    else:
        notes.append("no SOUL.md — the snapshot carries no persona")
    # SOUL is prose an operator wrote; it is exactly where a pasted token would hide, and
    # the structural strip has no purchase on it.
    soul_text = _redact_text(soul_text, "SOUL.md", pattern_hits)

    lock = _read_json(plugins_lock)
    plugins = lock.get("plugins") if isinstance(lock.get("plugins"), list) else []

    required = _dedupe(config_reqs + list(plugin_requirements or []))

    stamp = (now or datetime.now(UTC)).isoformat()
    manifest = {
        "snapshot_version": SNAPSHOT_VERSION,
        "kind": "agent-snapshot",
        "exported_at": stamp,
        "agent": {"name": agent_name},
        # Pins only — never plugin CODE. Import re-installs by resolved SHA (ADR 0027/0058),
        # which is what makes the artifact small, auditable, and reproducible.
        "plugins": plugins,
        "config": clean_config,
        "soul": "SOUL.md" if soul_text else None,
        "required_secrets": [r.as_dict() for r in required],
        # Stated in the artifact so a reader knows what it is NOT: ADR 0091 D4 seeds runtime
        # history empty, and this slice carries no knowledge/memory seed at all (#2105).
        # An opt-in knowledge seed (#2105) travels as domain-tagged docs under knowledge/.
        # Recorded here so import can find and re-ingest them without guessing.
        "knowledge": {"domains": dict(knowledge.counts)} if knowledge and knowledge.docs else None,
        "excludes": {
            "runtime_state": "checkpoints, telemetry, activity, inbox, a2a, scheduler, background — a snapshot yields a FRESH agent",
            "credentials": sorted(EXCLUDED_FILENAMES),
            "memory": "never included — what the agent recalls about a person's sessions is a different consent question",
        },
    }

    import yaml

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(SNAPSHOT_MANIFEST, yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
        if soul_text:
            zf.writestr("SOUL.md", soul_text)
        for label, root in (skills_dirs or {}).items():
            files = _skill_files(root)
            if not files:
                continue
            for rel, path in files:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    # A binary or unreadable asset can't be swept, so it can't be proven
                    # clean — skip it and SAY SO rather than ship an unexamined blob.
                    notes.append(f"skipped unreadable skill asset: {label}/{rel}")
                    continue
                zf.writestr(f"skills/{label}/{rel}", _redact_text(text, f"skills/{label}/{rel}", pattern_hits))
        for domain, body in sorted((knowledge.docs if knowledge else {}).items()):
            if not body.strip():
                continue
            # Swept like everything else: knowledge is exactly where a pasted credential
            # would sit unnoticed, and it is the largest body of free text in the artifact.
            zf.writestr(
                f"knowledge/{_safe_domain(domain)}.md",
                _redact_text(body, f"knowledge/{domain}", pattern_hits),
            )
        # Written LAST: the skill walk above can append notes and pattern hits, and the
        # review has to describe the finished artifact, not a partial one.
        zf.writestr(
            "REVIEW.md",
            render_review(
                agent_name=agent_name,
                required=required,
                pattern_redactions=pattern_hits,
                notes=notes,
                exported_at=stamp,
                knowledge=(knowledge.counts if knowledge else {}),
            ),
        )

    safe_name = "".join(c for c in agent_name if c.isalnum() or c in "-_") or "agent"
    filename = f"{safe_name}-snapshot-{(now or datetime.now(UTC)).strftime('%Y%m%d-%H%M%S')}.zip"
    return SnapshotResult(
        knowledge=dict(knowledge.counts) if knowledge and knowledge.docs else {},
        data=buf.getvalue(),
        filename=filename,
        manifest=manifest,
        required_secrets=required,
        pattern_redactions=pattern_hits,
        notes=notes,
    )


def collect_knowledge_seed(store, *, domains: list[str] | None = None, max_chars: int = 2_000_000) -> KnowledgeSeed:
    """Read a knowledge store into an exportable seed (#2105).

    Separate from ``build_snapshot`` because it is the one part that needs a live store —
    keeping it out means the builder stays host-free and the seed can be constructed in a
    test without sqlite.

    **Memory domains are dropped unconditionally** (``MEMORY_DOMAINS``), whatever the caller
    asks for. What an agent recalls about a person's sessions is not knowledge about a
    subject, and a snapshot is a thing you hand to someone else. A caller cannot opt into it
    by naming the domain, because the decision is not the caller's to make here.

    ``max_chars`` is a backstop against an unbounded export: a mature agent's knowledge base
    can be very large, and a snapshot is meant to be a file you can hand over. Truncation is
    reported by the caller rather than silently swallowing content.
    """
    seed = KnowledgeSeed()
    try:
        stats = store.stats() or {}
    except Exception:  # noqa: BLE001 — a broken store must not fail the whole export
        log.warning("[snapshot] knowledge stats failed — exporting no seed", exc_info=True)
        return seed

    wanted = [d for d in (domains or [k for k in stats if k != "total"]) if d not in MEMORY_DOMAINS]
    used = 0
    for domain in sorted(wanted):
        try:
            chunks = store.list_chunks(domain=domain, limit=10_000)
        except Exception:  # noqa: BLE001 — skip a domain that won't read, keep the rest
            log.warning("[snapshot] could not read knowledge domain %r", domain, exc_info=True)
            continue
        parts: list[str] = []
        for chunk in chunks:
            body = (getattr(chunk, "content", "") or "").strip()
            if not body:
                continue
            heading = (getattr(chunk, "heading", "") or "").strip()
            # Headings survive as markdown so a re-ingest chunks along the same seams the
            # source did, instead of one undifferentiated wall of text.
            piece = f"## {heading}\n\n{body}\n" if heading else f"{body}\n"
            if used + len(piece) > max_chars:
                break
            parts.append(piece)
            used += len(piece)
        if parts:
            seed.docs[domain] = "\n".join(parts)
            seed.counts[domain] = len(parts)
    return seed
