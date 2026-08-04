"""Agent snapshot import — rehydrate a fresh agent from a snapshot zip (ADR 0091 D3, #2104).

The counterpart to ``snapshot_op``. Where export's hazard is *leaking* (an artifact that
must be safe to publish), import's hazard is *executing*: a snapshot is a file someone
hands you, and applying it clones plugin repos and enables their code in-process. Export is
read-only on a machine you own; import runs a recipe you were given.

So this module is built around one shape: **inspect, then commit.**

``inspect_snapshot`` parses and validates with **no side effects** and returns an
``ImportPlan`` naming everything that will happen — which plugin URLs will be installed and
whether their source is recognized, which capabilities the config grants, which credentials
the operator must supply. ``apply_snapshot`` refuses to run without ``acknowledged=True``.
One deliberate decision that names every URL, rather than an "Import" click that silently
fans out into N code-execution decisions.

The config is applied **verbatim** — it IS the agent's definition, and stripping capability
keys would produce a duplicate that behaves subtly differently for reasons no review could
fully enumerate. Consistent with ADR 0071 D1 (trust, not sandbox): the answer to dangerous
config is to show it, not to silently neuter it. ``ImportPlan.capabilities`` is what makes
"show it" real.

Two things this deliberately does NOT do:

* **Never reads or writes a source ``secrets.yaml``.** ADR 0091 D3 is explicit that this is
  a NEW entry into the scaffold, not ``manager.create(from_config=…)``, which copies
  ``secrets.yaml`` verbatim — the exact opposite of the property Slice 1 exists to provide.
  Credentials arrive only as operator-supplied values at import time.
* **Never seeds runtime history.** A snapshot yields a *fresh* agent, not a resumed one
  (ADR 0091 D4). Knowledge/memory seeding is opt-in and belongs to #2105.

Host-free: paths and bytes in, no ``STATE``, no ``server`` import — the ``snapshot_op`` /
``export_op`` shape, so the round-trip is testable end to end without a running instance.
"""

from __future__ import annotations

import io
import logging
import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SNAPSHOT_MANIFEST = "agent.snapshot.yaml"
SUPPORTED_VERSIONS = frozenset({1})

#: Hard ceiling on what we will expand out of a zip. A snapshot is a recipe — manifest,
#: SOUL, SKILL.md files — so tens of KB is typical and 64 MB is already absurd. The cap is
#: against a zip bomb: `zipfile` decompresses on read, so a 40 KB archive can expand to
#: gigabytes and take the process down before anything gets to inspect it.
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 2000

#: Config keys whose presence GRANTS the agent a capability rather than describing it.
#: Surfaced in the plan so an operator sees what a snapshot switches on. Each entry is
#: (dotted path, human description). Not a blocklist — these are applied verbatim.
CAPABILITY_KEYS: tuple[tuple[str, str], ...] = (
    ("filesystem.allow_run", "shell command execution (the `run_command` tool)"),
    ("operator.allowed_dirs", "filesystem access outside the workspace"),
    ("operator.project_dir", "a project directory the agent operates in"),
    ("mcp.servers", "external MCP server processes"),
    ("delegates", "outbound calls to other agents / coding agents"),
    ("plugins.sources.allow", "which plugin sources this agent may install from"),
)


class SnapshotError(Exception):
    """A snapshot that cannot be trusted or understood. The message reaches an operator."""


@dataclass
class PluginPin:
    id: str
    url: str
    ref: str = ""
    #: Whether ``url`` matches a source the importing box already recognizes. Advisory —
    #: it sharpens the ack ("2 of these are from somewhere you haven't installed from
    #: before") rather than gating, because a legitimate snapshot from a teammate's org is
    #: unrecognized and still perfectly fine to accept knowingly.
    recognized: bool = False

    def as_dict(self) -> dict:
        return {"id": self.id, "url": self.url, "ref": self.ref, "recognized": self.recognized}


@dataclass
class ImportPlan:
    """Everything applying this snapshot would do — computed without doing any of it."""

    agent_name: str
    plugins: list[PluginPin] = field(default_factory=list)
    required_secrets: list[dict] = field(default_factory=list)
    #: (dotted key, description) for each capability the config grants.
    capabilities: list[tuple[str, str]] = field(default_factory=list)
    has_soul: bool = False
    skill_files: int = 0
    mcp_servers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def unrecognized_plugins(self) -> list[PluginPin]:
        return [p for p in self.plugins if not p.recognized]

    def as_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "plugins": [p.as_dict() for p in self.plugins],
            "required_secrets": self.required_secrets,
            "capabilities": [{"key": k, "grants": d} for k, d in self.capabilities],
            "has_soul": self.has_soul,
            "skill_files": self.skill_files,
            "mcp_servers": self.mcp_servers,
            "notes": self.notes,
            "runs_code": bool(self.plugins),
        }


def _safe_member(name: str) -> str:
    """Normalize a zip member name, refusing anything that could escape the extract root.

    Zip-slip: an archive member named ``../../.ssh/authorized_keys`` extracts OUTSIDE the
    destination on a naive ``extractall``. A snapshot is attacker-controllable in exactly
    the case the feature is designed for (someone hands you one), so every member name is
    normalized and rejected here rather than trusted. Absolute paths, parent traversal and
    drive letters are all refused.
    """
    if not name or name.endswith("/"):
        return ""
    cleaned = name.replace("\\", "/")
    if cleaned.startswith("/") or ":" in cleaned.split("/")[0]:
        raise SnapshotError(f"unsafe path in snapshot: {name!r}")
    normalized = posixpath.normpath(cleaned)
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        raise SnapshotError(f"unsafe path in snapshot: {name!r}")
    return normalized


def _open(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SnapshotError("not a valid zip archive") from exc
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise SnapshotError(f"snapshot has {len(infos)} members (max {MAX_MEMBERS})")
    total = sum(i.file_size for i in infos)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise SnapshotError(f"snapshot expands to {total} bytes (max {MAX_UNCOMPRESSED_BYTES})")
    for info in infos:
        _safe_member(info.filename)  # raises on traversal
    return zf


def _dotted(doc: dict, path: str) -> Any:
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _recognized(url: str, known_sources: list[str] | None) -> bool:
    """Whether ``url`` looks like a source this box already installs from.

    Substring match on the configured allowlist plus the first-party org. Deliberately
    loose: this only decides whether the ack says "unfamiliar source", and a false
    *negative* (flagging a fine URL) costs a moment's attention while a false positive
    would quietly reassure. Erring toward flagging is the right bias.
    """
    candidates = list(known_sources or []) + ["github.com/protoLabsAI/"]
    lowered = url.lower()
    return any(c.strip().lower().strip("*") in lowered for c in candidates if c.strip())


def inspect_snapshot(data: bytes, *, known_sources: list[str] | None = None) -> ImportPlan:
    """Parse + validate a snapshot and describe what importing it would do. NO side effects.

    Raises ``SnapshotError`` on anything malformed, unsafe, or from a version this build
    does not understand — before a single file is written or a single repo is cloned.
    """
    import yaml

    zf = _open(data)
    names = {_safe_member(i.filename): i.filename for i in zf.infolist() if _safe_member(i.filename)}
    if SNAPSHOT_MANIFEST not in names:
        raise SnapshotError(f"no {SNAPSHOT_MANIFEST} — this is not an agent snapshot")

    try:
        manifest = yaml.safe_load(zf.read(names[SNAPSHOT_MANIFEST]).decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SnapshotError(f"unreadable {SNAPSHOT_MANIFEST}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SnapshotError(f"{SNAPSHOT_MANIFEST} is not a mapping")

    version = manifest.get("snapshot_version")
    if version not in SUPPORTED_VERSIONS:
        raise SnapshotError(
            f"snapshot version {version!r} is not supported by this build "
            f"(understands {sorted(SUPPORTED_VERSIONS)}) — upgrade protoAgent to import it"
        )
    if str(manifest.get("kind") or "") != "agent-snapshot":
        raise SnapshotError("manifest `kind` is not `agent-snapshot`")

    config = manifest.get("config")
    config = config if isinstance(config, dict) else {}

    plugins: list[PluginPin] = []
    for raw in manifest.get("plugins") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        if not url:
            # A builtin entry (no url) installs nothing — skip rather than surface a pin
            # the operator can't evaluate.
            continue
        plugins.append(
            PluginPin(
                id=str(raw.get("id") or "").strip() or url.rstrip("/").split("/")[-1],
                url=url,
                ref=str(raw.get("sha") or raw.get("ref") or "").strip(),
                recognized=_recognized(url, known_sources),
            )
        )

    capabilities = [(key, desc) for key, desc in CAPABILITY_KEYS if _dotted(config, key)]
    servers = _dotted(config, "mcp.servers")
    mcp_servers = [str(s.get("name") or "?") for s in servers if isinstance(s, dict)] if isinstance(servers, list) else []

    notes: list[str] = []
    unpinned = [p.id for p in plugins if not p.ref]
    if unpinned:
        # ADR 0091 D1 says plugins travel as url + RESOLVED SHA. An unpinned entry installs
        # whatever the branch tip happens to be today, which is not the agent that was
        # exported — say so rather than silently reproducing something else.
        notes.append(f"unpinned plugin(s) will install at branch tip, not the exported commit: {', '.join(unpinned)}")

    return ImportPlan(
        agent_name=str((manifest.get("agent") or {}).get("name") or "").strip() or "imported-agent",
        plugins=plugins,
        required_secrets=[r for r in (manifest.get("required_secrets") or []) if isinstance(r, dict)],
        capabilities=capabilities,
        has_soul=bool(manifest.get("soul")) and "SOUL.md" in names,
        skill_files=sum(1 for n in names if n.startswith("skills/")),
        mcp_servers=mcp_servers,
        notes=notes,
    )


def stage_snapshot(data: bytes, dest: Path) -> dict:
    """Expand a snapshot's *definition files* into ``dest`` and return its manifest.

    Writes only what a scaffold needs — the config, SOUL and skill trees — through
    ``_safe_member``, so no member can land outside ``dest``. Called after the plan has
    been acknowledged.
    """
    import yaml

    zf = _open(data)
    dest.mkdir(parents=True, exist_ok=True)
    manifest: dict = {}
    for info in zf.infolist():
        rel = _safe_member(info.filename)
        if not rel:
            continue
        if rel == SNAPSHOT_MANIFEST:
            manifest = yaml.safe_load(zf.read(info.filename).decode("utf-8")) or {}
            continue
        if rel != "SOUL.md" and not rel.startswith("skills/"):
            continue  # REVIEW.md and anything else is documentation, not definition
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zf.read(info.filename))

    config = manifest.get("config") if isinstance(manifest.get("config"), dict) else {}
    (dest / "langgraph-config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return manifest if isinstance(manifest, dict) else {}


@dataclass
class ImportResult:
    """What actually happened. Partial success is normal and must be legible: the agent can
    exist and be usable while a plugin failed to clone or a credential is still missing."""

    name: str
    workspace_id: str
    path: str
    installed: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    missing_secrets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether the agent is ready to run. False means it exists but needs the operator —
        ADR 0091 D3's "incomplete until the required_secrets are provided"."""
        return not self.failed and not self.missing_secrets

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "workspace_id": self.workspace_id,
            "path": self.path,
            "installed": self.installed,
            "failed": self.failed,
            "missing_secrets": self.missing_secrets,
            "notes": self.notes,
            "complete": self.complete,
        }


def apply_snapshot(
    data: bytes,
    *,
    name: str,
    acknowledged: bool,
    secrets: dict[str, str] | None = None,
    plan: ImportPlan | None = None,
    install: bool = True,
) -> ImportResult:
    """Stand up a fresh agent from a snapshot. Refuses without ``acknowledged``.

    ``acknowledged`` is not ceremony. Applying a snapshot clones the repos it names and
    enables their code in-process; the caller must have shown the operator ``inspect_snapshot``'s
    plan — every URL, every capability the config grants — and gotten a yes. A default-False
    flag means a caller that forgets cannot silently execute a stranger's plugin list.

    ``secrets`` maps a ``required_secrets`` name to its value and is written to the NEW
    workspace's overlay only. Anything unsupplied lands in ``missing_secrets`` rather than
    failing the import — a half-configured agent the operator can finish beats no agent.
    """
    if not acknowledged:
        raise SnapshotError(
            "import not acknowledged — applying a snapshot installs and runs the plugin code it "
            "names. Show the operator inspect_snapshot()'s plan and pass acknowledged=True."
        )
    from graph.workspaces import manager

    plan = plan or inspect_snapshot(data)
    notes = list(plan.notes)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="protoagent-snapshot-") as tmp:
        staged = Path(tmp)
        stage_snapshot(data, staged)  # manifest re-read from the plan; we need the files
        soul_text = ""
        soul_file = staged / "SOUL.md"
        if soul_file.exists():
            try:
                soul_text = soul_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                notes.append("SOUL.md could not be read — the agent keeps the default persona")

        rec = manager.create(
            name,
            snapshot_config=staged / "langgraph-config.yaml",
            soul=soul_text or None,
        )
        ws = Path(rec["path"])

        # Skills re-seed from disk each boot (ADR 0060), so copying the trees IS the
        # restore. Staged through _safe_member already, so no member can land outside ws.
        copied = _copy_skills(staged / "skills", ws)
        if copied:
            notes.append(f"seeded {copied} skill file(s)")

    installed, failed = ([], [])
    if install and plan.plugins:
        installed, failed = _install_pins(ws, plan.plugins)

    missing = _write_secrets(ws, plan, secrets or {})
    return ImportResult(
        name=rec["name"],
        workspace_id=rec["id"],
        path=str(ws),
        installed=installed,
        failed=failed,
        missing_secrets=missing,
        notes=notes,
    )


def _copy_skills(src: Path, ws: Path) -> int:
    """Copy staged skill trees into the workspace. ``skills/<label>/...`` collapses onto the
    workspace's own ``skills/`` — the label was only an export-time grouping of the two
    source dirs, and the target has one."""
    if not src.is_dir():
        return 0
    import shutil

    count = 0
    for label_dir in sorted(p for p in src.iterdir() if p.is_dir()):
        for path in sorted(label_dir.rglob("*")):
            if not path.is_file():
                continue
            out = ws / "skills" / path.relative_to(label_dir)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, out)
            count += 1
    return count


def _install_pins(ws: Path, pins: list[PluginPin]) -> tuple[list[str], list[dict]]:
    """Clone each pinned plugin into the new workspace, at its exported SHA.

    Scoped by ``PROTOAGENT_HOME=<ws>`` exactly like the fleet's bundle install, so plugins
    land in the NEW agent's tree and can't touch the importing instance's. A failure is
    recorded and the rest continue: one unreachable repo shouldn't cost the operator the
    whole import, and ``ImportResult.complete`` already reports the agent as unfinished.
    """
    import os
    import subprocess

    from graph.workspaces.manager import _server_argv

    env = {**os.environ, "PROTOAGENT_HOME": str(ws)}
    installed: list[str] = []
    failed: list[dict] = []
    for pin in pins:
        argv = [*_server_argv(), "plugin", "install", pin.url]
        if pin.ref:
            argv += ["--ref", pin.ref]
        try:
            proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            failed.append({"id": pin.id, "url": pin.url, "error": "install timed out after 300s"})
            continue
        if proc.returncode != 0:
            failed.append({"id": pin.id, "url": pin.url, "error": (proc.stderr or proc.stdout).strip()[-300:]})
            continue
        installed.append(pin.id)

    if installed:
        try:
            from graph.workspaces.manager import _enable_installed_in_config

            _enable_installed_in_config(ws / "config" / "langgraph-config.yaml", ws / "plugins.lock")
        except Exception:  # noqa: BLE001 — installed but not enabled is recoverable in Settings
            log.warning("[snapshot] enabling imported plugins failed", exc_info=True)
            failed.append({"id": "*", "url": "", "error": "installed, but auto-enable failed — enable in Settings ▸ Plugins"})
    return installed, failed


def _write_secrets(ws: Path, plan: ImportPlan, supplied: dict[str, str]) -> list[str]:
    """Write operator-supplied credentials into the NEW workspace's overlay, and report
    which of the snapshot's ``required_secrets`` are still missing.

    ``was_set`` is the signal that matters: a credential the exporting agent actually had is
    one the target genuinely needs, while a merely-declared one may never have been used. So
    an unsupplied declared-but-unset entry is not reported as missing — that would tell the
    operator their import is broken when it is in exactly the state the source was in.
    """
    import yaml

    doc: dict[str, dict[str, str]] = {}
    for name, value in (supplied or {}).items():
        if not str(value or "").strip():
            continue
        section, _, key = str(name).partition(".")
        if not section or not key:
            continue
        # `mcp.<server>.env.<VAR>` is a config-shaped path, not a secrets.yaml section —
        # it belongs in the MCP block, which is #2104's follow-up surface. Skip rather than
        # write a nonsense `mcp` section that shadows nothing.
        if section == "mcp":
            continue
        doc.setdefault(section, {})[key] = str(value)

    if doc:
        path = ws / "config" / "secrets.yaml"
        existing = {}
        if path.exists():
            try:
                existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                existing = {}
        if not isinstance(existing, dict):
            existing = {}
        for section, values in doc.items():
            existing.setdefault(section, {}).update(values)
        path.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")
        path.chmod(0o600)

    provided = set(supplied or {})
    return sorted(
        str(r.get("name"))
        for r in plan.required_secrets
        if r.get("was_set") and str(r.get("name")) not in provided
    )
