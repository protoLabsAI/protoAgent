"""Agent snapshot import (ADR 0091 D3, #2104).

Export's hazard is leaking; import's is **executing**. A snapshot is a file someone hands
you, and applying it clones the repos it names and enables their code in-process. So the
tests here are weighted toward the ways a hostile or malformed archive could do something
the operator didn't agree to — zip-slip, zip-bomb, an unknown version, an unacknowledged
apply — plus the round-trip that proves a real export actually rehydrates.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from graph.snapshot_import import (
    MAX_MEMBERS,
    ImportPlan,
    SnapshotError,
    apply_snapshot,
    inspect_snapshot,
    stage_snapshot,
)
from graph.snapshot_op import build_snapshot
from tests.privacy_asserts import assert_owner_only

SECRET_KEYS = (("model", "api_key"), ("discord", "bot_token"))


def _zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _manifest(**over) -> str:
    doc = {
        "snapshot_version": 1,
        "kind": "agent-snapshot",
        "agent": {"name": "vera"},
        "plugins": [{"id": "github", "url": "https://github.com/protoLabsAI/github-plugin", "sha": "abc123"}],
        "config": {"model": {"name": "protolabs/reasoning"}},
        "soul": "SOUL.md",
        "required_secrets": [{"name": "model.api_key", "kind": "config", "description": "", "was_set": True}],
    }
    doc.update(over)
    return yaml.safe_dump(doc)


def _snapshot(**over) -> bytes:
    return _zip({"agent.snapshot.yaml": _manifest(**over), "SOUL.md": "# Vera\n\nYou review PRs.\n"})


def _snapshot_no_plugins(**over) -> bytes:
    """A snapshot whose plugin list is empty.

    Any test that APPLIES must use this: the real apply path shells out to
    `plugin install <url>`, which clones over the network. A test that leaves the default
    pin in place quietly makes a real request to github.com — slow, flaky, and dependent on
    CI having egress. Ask for the install path to be exercised in a live smoke, not here.
    """
    over.setdefault("plugins", [])
    return _snapshot(**over)


# ── refusing what shouldn't be trusted ───────────────────────────────────────────────


class TestHostileArchives:
    def test_zip_slip_is_refused(self):
        """A member named ../../x escapes the destination on a naive extractall. The
        snapshot is attacker-controllable in exactly the case the feature exists for."""
        data = _zip({"agent.snapshot.yaml": _manifest(), "../../evil.txt": "pwned"})
        with pytest.raises(SnapshotError, match="unsafe path"):
            inspect_snapshot(data)

    def test_absolute_path_member_is_refused(self):
        data = _zip({"agent.snapshot.yaml": _manifest(), "/etc/passwd": "x"})
        with pytest.raises(SnapshotError, match="unsafe path"):
            inspect_snapshot(data)

    def test_windows_style_traversal_is_refused(self):
        data = _zip({"agent.snapshot.yaml": _manifest(), "..\\..\\evil.txt": "x"})
        with pytest.raises(SnapshotError, match="unsafe path"):
            inspect_snapshot(data)

    def test_zip_bomb_is_refused_before_reading(self):
        """zipfile decompresses on read, so a small archive can expand to gigabytes. The
        cap is checked against the declared sizes BEFORE any member is read."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("agent.snapshot.yaml", _manifest())
            zf.writestr("bomb", "\0" * (65 * 1024 * 1024))
        with pytest.raises(SnapshotError, match="expands to"):
            inspect_snapshot(buf.getvalue())

    def test_too_many_members_is_refused(self):
        data = _zip({"agent.snapshot.yaml": _manifest(), **{f"skills/f{i}": "x" for i in range(MAX_MEMBERS + 1)}})
        with pytest.raises(SnapshotError, match="members"):
            inspect_snapshot(data)

    def test_not_a_zip(self):
        with pytest.raises(SnapshotError, match="valid zip"):
            inspect_snapshot(b"definitely not a zip")

    def test_missing_manifest(self):
        with pytest.raises(SnapshotError, match="not an agent snapshot"):
            inspect_snapshot(_zip({"README.md": "hi"}))

    def test_future_version_is_refused_with_an_actionable_message(self):
        """Refusing beats best-effort: a v2 manifest may mean something different by the
        same keys, and half-applying it produces an agent nobody designed."""
        with pytest.raises(SnapshotError, match="upgrade protoAgent"):
            inspect_snapshot(_snapshot(snapshot_version=99))

    def test_wrong_kind_is_refused(self):
        with pytest.raises(SnapshotError, match="kind"):
            inspect_snapshot(_snapshot(kind="something-else"))

    def test_malformed_yaml_is_refused(self):
        with pytest.raises(SnapshotError, match="unreadable"):
            inspect_snapshot(_zip({"agent.snapshot.yaml": "{{{ not yaml"}))


# ── the plan: what would happen, before anything happens ─────────────────────────────


class TestInspect:
    def test_names_every_plugin_that_would_be_installed(self):
        plan = inspect_snapshot(_snapshot())
        assert [p.url for p in plan.plugins] == ["https://github.com/protoLabsAI/github-plugin"]
        assert plan.plugins[0].ref == "abc123"
        assert plan.as_dict()["runs_code"] is True

    def test_flags_an_unfamiliar_source(self):
        """The ack should be able to say "2 of these are from somewhere you haven't
        installed from before" — flagging is advisory, not a gate."""
        plan = inspect_snapshot(
            _snapshot(plugins=[{"id": "x", "url": "https://gitlab.example/acme/x", "sha": "d"}])
        )
        assert plan.unrecognized_plugins
        assert plan.plugins[0].recognized is False

    def test_respects_a_configured_allowlist_when_judging_familiarity(self):
        plan = inspect_snapshot(
            _snapshot(plugins=[{"id": "x", "url": "https://gitlab.example/acme/x", "sha": "d"}]),
            known_sources=["gitlab.example/acme/*"],
        )
        assert plan.plugins[0].recognized is True

    def test_surfaces_capability_granting_config(self):
        """The config is applied verbatim (ADR 0071 D1 — trust, not sandbox), so the whole
        control is showing what it switches on."""
        plan = inspect_snapshot(
            _snapshot(
                config={
                    "filesystem": {"allow_run": True},
                    "operator": {"allowed_dirs": ["/srv/x"]},
                    "mcp": {"servers": [{"name": "github"}]},
                }
            )
        )
        keys = [k for k, _ in plan.capabilities]
        assert "filesystem.allow_run" in keys
        assert "operator.allowed_dirs" in keys
        assert "mcp.servers" in keys
        assert plan.mcp_servers == ["github"]

    def test_a_plain_config_grants_nothing(self):
        assert inspect_snapshot(_snapshot()).capabilities == []

    def test_warns_when_a_plugin_is_unpinned(self):
        """An unpinned entry installs the branch tip, which is not the agent that was
        exported. Say so rather than silently reproducing something else."""
        plan = inspect_snapshot(_snapshot(plugins=[{"id": "x", "url": "https://github.com/protoLabsAI/x"}]))
        assert any("branch tip" in n for n in plan.notes)

    def test_builtin_entries_are_not_shown_as_installs(self):
        plan = inspect_snapshot(_snapshot(plugins=[{"id": "delegates", "builtin": True}]))
        assert plan.plugins == []
        assert plan.as_dict()["runs_code"] is False

    def test_inspect_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        inspect_snapshot(_snapshot())
        assert list(tmp_path.iterdir()) == []


# ── the ack gate ─────────────────────────────────────────────────────────────────────


class TestAcknowledgement:
    def test_apply_refuses_without_acknowledgement(self):
        """Default-False means a caller that forgets cannot silently execute a stranger's
        plugin list."""
        with pytest.raises(SnapshotError, match="not acknowledged"):
            apply_snapshot(_snapshot(), name="x", acknowledged=False)

    def test_the_refusal_explains_what_the_caller_must_do(self):
        with pytest.raises(SnapshotError) as exc:
            apply_snapshot(_snapshot(), name="x", acknowledged=False)
        assert "inspect_snapshot" in str(exc.value)
        assert "runs the plugin code" in str(exc.value)


# ── staging ──────────────────────────────────────────────────────────────────────────


class TestStaging:
    def test_writes_only_definition_files(self, tmp_path):
        data = _zip(
            {
                "agent.snapshot.yaml": _manifest(),
                "SOUL.md": "# soul",
                "skills/instance/a/SKILL.md": "# skill",
                "REVIEW.md": "# review",  # documentation, not definition
            }
        )
        stage_snapshot(data, tmp_path)
        assert (tmp_path / "SOUL.md").exists()
        assert (tmp_path / "skills" / "instance" / "a" / "SKILL.md").exists()
        assert not (tmp_path / "REVIEW.md").exists()

    def test_materializes_the_config_from_the_manifest(self, tmp_path):
        stage_snapshot(_snapshot(config={"model": {"name": "m"}}), tmp_path)
        doc = yaml.safe_load((tmp_path / "langgraph-config.yaml").read_text())
        assert doc == {"model": {"name": "m"}}

    def test_staging_cannot_escape_the_destination(self, tmp_path):
        data = _zip({"agent.snapshot.yaml": _manifest(), "../escape.txt": "x"})
        with pytest.raises(SnapshotError):
            stage_snapshot(data, tmp_path / "dest")


# ── round trip: a REAL export rehydrates ─────────────────────────────────────────────


class TestRoundTrip:
    """ADR 0091's acceptance bar for this slice: agent A → snapshot → agent B ≈ A, minus
    history and secrets. Built from `snapshot_op.build_snapshot`, not a hand-written zip,
    so the two halves are pinned to each other rather than to my idea of the format."""

    @pytest.fixture
    def exported(self, tmp_path):
        cfg = tmp_path / "src" / "config"
        cfg.mkdir(parents=True)
        (cfg / "langgraph-config.yaml").write_text(
            yaml.safe_dump(
                {
                    "identity": {"name": "vera"},
                    "model": {"name": "protolabs/reasoning", "api_key": "sk-" + "x" * 30},
                    "behavior": {"temperature": 0.4},
                    "filesystem": {"allow_run": True},
                }
            ),
            encoding="utf-8",
        )
        (cfg / "SOUL.md").write_text("# Vera\n\nYou review PRs carefully.\n", encoding="utf-8")
        (tmp_path / "src" / "plugins.lock").write_text('{"plugins": []}', encoding="utf-8")
        skills = tmp_path / "src" / "skills" / "reviewing"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("# Reviewing\n\nCheck the diff.\n", encoding="utf-8")
        return build_snapshot(
            config_yaml=cfg / "langgraph-config.yaml",
            soul_path=cfg / "SOUL.md",
            plugins_lock=tmp_path / "src" / "plugins.lock",
            skills_dirs={"instance": tmp_path / "src" / "skills"},
            agent_name="vera",
            secret_key_paths=SECRET_KEYS,
            plugin_requirements=[],
            now=datetime(2026, 8, 5, tzinfo=UTC),
        ).data

    def test_a_real_export_inspects_cleanly(self, exported):
        plan = inspect_snapshot(exported)
        assert plan.agent_name == "vera"
        assert plan.has_soul is True
        assert plan.skill_files == 1
        # The export stripped model.api_key and inventoried it; import must ask for it.
        assert any(r["name"] == "model.api_key" and r["was_set"] for r in plan.required_secrets)
        # …and the capability the source config granted is surfaced, not silently applied.
        assert "filesystem.allow_run" in [k for k, _ in plan.capabilities]

    def test_staging_a_real_export_reproduces_the_definition(self, exported, tmp_path):
        dest = tmp_path / "staged"
        stage_snapshot(exported, dest)
        doc = yaml.safe_load((dest / "langgraph-config.yaml").read_text())
        assert doc["model"]["name"] == "protolabs/reasoning"
        assert doc["behavior"]["temperature"] == 0.4
        assert "api_key" not in doc.get("model", {})  # the export stripped it; import can't resurrect it
        assert "You review PRs carefully." in (dest / "SOUL.md").read_text()
        assert (dest / "skills" / "instance" / "reviewing" / "SKILL.md").exists()


# ── missing-secret reporting ─────────────────────────────────────────────────────────


class TestMissingSecrets:
    def _plan(self, *required) -> ImportPlan:
        return ImportPlan(agent_name="x", required_secrets=list(required))

    def test_a_credential_the_source_HAD_is_reported_missing(self, tmp_path):
        from graph.snapshot_import import _write_secrets

        (tmp_path / "config").mkdir()
        plan = self._plan({"name": "model.api_key", "was_set": True})
        assert _write_secrets(tmp_path, plan, {}) == ["model.api_key"]

    def test_a_merely_declared_credential_is_not_reported_missing(self, tmp_path):
        """`was_set: False` means the SOURCE agent didn't have one either. Reporting it as
        missing would tell the operator their import is broken when it is in exactly the
        state the original was in."""
        from graph.snapshot_import import _write_secrets

        (tmp_path / "config").mkdir()
        plan = self._plan({"name": "discord.bot_token", "was_set": False})
        assert _write_secrets(tmp_path, plan, {}) == []

    def test_supplied_credentials_land_in_the_new_overlay_only(self, tmp_path):
        from graph.snapshot_import import _write_secrets

        (tmp_path / "config").mkdir()
        plan = self._plan({"name": "model.api_key", "was_set": True})
        assert _write_secrets(tmp_path, plan, {"model.api_key": "sk-live"}) == []
        doc = yaml.safe_load((tmp_path / "config" / "secrets.yaml").read_text())
        assert doc["model"]["api_key"] == "sk-live"

    def test_the_overlay_is_owner_only(self, tmp_path):
        from graph.snapshot_import import _write_secrets

        (tmp_path / "config").mkdir()
        _write_secrets(tmp_path, self._plan(), {"model.api_key": "sk-live"})
        # Owner-only, portably (0o600 on POSIX / owner-only ACL on Windows).
        assert_owner_only(tmp_path / "config" / "secrets.yaml")

    def test_a_blank_value_is_not_written(self, tmp_path):
        from graph.snapshot_import import _write_secrets

        (tmp_path / "config").mkdir()
        _write_secrets(tmp_path, self._plan(), {"model.api_key": "   "})
        assert not (tmp_path / "config" / "secrets.yaml").exists()


# ── end to end: the slice's acceptance criterion ─────────────────────────────────────


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    """Isolated workspaces dir, mirroring tests/test_workspaces.py."""
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    return tmp_path / "ws"


class TestApplyEndToEnd:
    """ADR 0091's Slice-2 acceptance: import a Slice-1 snapshot → a fresh instance with the
    right SOUL, config and skills, incomplete until its credentials are supplied.

    `install=False` throughout: cloning real repos belongs to a live smoke, not a unit test,
    and the ack gate + pin plumbing are covered separately. What's asserted here is the
    SCAFFOLD — including the property the whole slice exists for, that no credential from
    anywhere reaches the new agent.
    """

    def test_creates_a_fresh_agent_with_the_snapshot_definition(self, ws_root):
        res = apply_snapshot(
            _snapshot_no_plugins(config={"model": {"name": "protolabs/reasoning"}, "behavior": {"temperature": 0.2}}),
            name="vera-copy",
            acknowledged=True,
            install=False,
        )
        ws = Path(res.path)
        doc = yaml.safe_load((ws / "config" / "langgraph-config.yaml").read_text())
        assert doc["model"]["name"] == "protolabs/reasoning"
        assert doc["behavior"]["temperature"] == 0.2
        assert "You review PRs." in (ws / "config" / "SOUL.md").read_text()

    def test_identity_is_restamped_to_the_new_name(self, ws_root):
        """Two agents from one snapshot must not collide on identity or data scope."""
        res = apply_snapshot(_snapshot_no_plugins(), name="vera-copy", acknowledged=True, install=False)
        doc = yaml.safe_load((Path(res.path) / "config" / "langgraph-config.yaml").read_text())
        assert doc["identity"]["name"] == "vera-copy"
        assert doc["instance"]["id"] == res.workspace_id

    def test_the_new_overlay_is_empty_no_credential_travels(self, ws_root):
        """The property the slice exists for. `create(from_config=…)` copies secrets.yaml
        verbatim; this path must never do that, so the overlay starts blank."""
        res = apply_snapshot(_snapshot_no_plugins(), name="vera-copy", acknowledged=True, install=False)
        overlay = Path(res.path) / "config" / "secrets.yaml"
        assert overlay.exists()
        assert not (yaml.safe_load(overlay.read_text()) or {})

    def test_arrives_incomplete_until_its_credentials_are_supplied(self, ws_root):
        res = apply_snapshot(_snapshot_no_plugins(), name="vera-copy", acknowledged=True, install=False)
        assert res.missing_secrets == ["model.api_key"]
        assert res.complete is False

    def test_supplying_the_credential_completes_it(self, ws_root):
        res = apply_snapshot(
            _snapshot_no_plugins(),
            name="vera-copy",
            acknowledged=True,
            install=False,
            secrets={"model.api_key": "sk-live-value"},
        )
        assert res.missing_secrets == []
        assert res.complete is True
        doc = yaml.safe_load((Path(res.path) / "config" / "secrets.yaml").read_text())
        assert doc["model"]["api_key"] == "sk-live-value"

    def test_skills_are_seeded_into_the_new_workspace(self, ws_root):
        data = _zip(
            {
                "agent.snapshot.yaml": _manifest(plugins=[]),
                "SOUL.md": "# s",
                "skills/instance/reviewing/SKILL.md": "# Reviewing\n",
            }
        )
        res = apply_snapshot(data, name="vera-copy", acknowledged=True, install=False)
        assert (Path(res.path) / "skills" / "reviewing" / "SKILL.md").exists()
        assert any("skill file" in n for n in res.notes)

    def test_a_second_import_of_the_same_snapshot_needs_a_different_name(self, ws_root):
        from graph.workspaces.manager import WorkspaceError

        apply_snapshot(_snapshot_no_plugins(), name="vera-copy", acknowledged=True, install=False)
        with pytest.raises(WorkspaceError, match="already exists"):
            apply_snapshot(_snapshot_no_plugins(), name="vera-copy", acknowledged=True, install=False)

    def test_no_runtime_history_is_seeded(self, ws_root):
        """A snapshot yields a FRESH agent, not a resumed one (ADR 0091 D4)."""
        res = apply_snapshot(_snapshot_no_plugins(), name="vera-copy", acknowledged=True, install=False)
        ws = Path(res.path)
        for store in ("checkpoints", "telemetry", "activity", "inbox", "a2a", "background", "knowledge"):
            assert not (ws / store).exists(), f"{store} should not be seeded"


# ── the REST surface ─────────────────────────────────────────────────────────────────


class TestImportRoute:
    """`POST /api/agent/import`. The route is thin, so what's asserted is the SHAPE that
    keeps it safe: no ack ⇒ plan only, malformed ⇒ 400 before anything touches disk."""

    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from operator_api.snapshot_routes import register_snapshot_routes

        app = FastAPI()
        register_snapshot_routes(app)
        return TestClient(app)

    def test_without_acknowledgement_it_only_plans(self, client, ws_root):
        res = client.post("/api/agent/import", files={"file": ("s.zip", _snapshot(), "application/zip")})
        assert res.status_code == 200
        body = res.json()
        assert body["mode"] == "plan"
        assert body["runs_code"] is True
        assert body["plugins"][0]["url"].endswith("github-plugin")
        assert not list(ws_root.glob("*")) if ws_root.exists() else True

    def test_a_hostile_archive_is_400_not_500(self, client):
        """Traversal must be a clean, explained refusal — a 500 reads as our bug and tells
        the operator nothing about the file they were handed."""
        bad = _zip({"agent.snapshot.yaml": _manifest(), "../../evil": "x"})
        res = client.post("/api/agent/import", files={"file": ("s.zip", bad, "application/zip")})
        assert res.status_code == 400
        assert "unsafe path" in res.json()["detail"]

    def test_an_unsupported_version_is_400_with_the_reason(self, client):
        res = client.post(
            "/api/agent/import", files={"file": ("s.zip", _snapshot(snapshot_version=99), "application/zip")}
        )
        assert res.status_code == 400
        assert "upgrade protoAgent" in res.json()["detail"]

    def test_acknowledged_applies_and_reports_incompleteness(self, client, ws_root):
        res = client.post(
            "/api/agent/import",
            files={"file": ("s.zip", _snapshot_no_plugins(), "application/zip")},
            data={"name": "vera-copy", "acknowledged": "true"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["mode"] == "applied"
        assert body["name"] == "vera-copy"
        assert body["missing_secrets"] == ["model.api_key"]
        assert body["complete"] is False

    def test_supplied_secrets_ride_the_form(self, client, ws_root):
        import json as _json

        res = client.post(
            "/api/agent/import",
            files={"file": ("s.zip", _snapshot_no_plugins(), "application/zip")},
            data={
                "name": "vera-copy",
                "acknowledged": "true",
                "secrets_json": _json.dumps({"model.api_key": "sk-live"}),
            },
        )
        body = res.json()
        assert body["complete"] is True
        doc = yaml.safe_load((Path(body["path"]) / "config" / "secrets.yaml").read_text())
        assert doc["model"]["api_key"] == "sk-live"

    def test_malformed_secrets_json_is_400(self, client, ws_root):
        res = client.post(
            "/api/agent/import",
            files={"file": ("s.zip", _snapshot_no_plugins(), "application/zip")},
            data={"acknowledged": "true", "secrets_json": "{{{"},
        )
        assert res.status_code == 400


# ── #2105: the knowledge seed, on the receiving end ──────────────────────────────────


class TestKnowledgeSeedImport:
    """A snapshot carrying knowledge is content, not just config — the importer is
    receiving someone's material and should be told so before applying it."""

    def _kb_snapshot(self) -> bytes:
        return _zip(
            {
                "agent.snapshot.yaml": _manifest(
                    plugins=[], knowledge={"domains": {"protocols": 12, "general": 3}}
                ),
                "SOUL.md": "# s",
                "knowledge/protocols.md": "## Blitzing\n\nOne blitz per turn.\n",
                "knowledge/general.md": "Notes.\n",
            }
        )

    def test_the_plan_reports_the_seed(self):
        plan = inspect_snapshot(self._kb_snapshot())
        assert plan.knowledge == {"protocols": 12, "general": 3}
        assert plan.as_dict()["carries_knowledge"] is True

    def test_a_definition_only_snapshot_carries_none(self):
        plan = inspect_snapshot(_snapshot_no_plugins())
        assert plan.knowledge == {}
        assert plan.as_dict()["carries_knowledge"] is False

    def test_seed_is_ingested_into_the_new_agent(self, ws_root):
        res = apply_snapshot(self._kb_snapshot(), name="vera-kb", acknowledged=True, install=False)
        ws = Path(res.path)
        # Searchable in the NEW agent's own store — the point of re-ingesting as text.
        from knowledge import KnowledgeStore

        store = KnowledgeStore(str(ws / "knowledge" / "agent.db"))
        hits = store.search("blitz", k=5)
        assert hits, "the seeded knowledge is not searchable in the imported agent"
        assert any("blitz" in str(h.get("content", "")).lower() for h in hits)

    def test_source_docs_are_kept_for_a_later_re_ingest(self, ws_root):
        """Embeddings need the target's own gateway, which may not be configured yet. The
        text stays so that can happen later without hunting for the original snapshot."""
        res = apply_snapshot(self._kb_snapshot(), name="vera-kb", acknowledged=True, install=False)
        assert (Path(res.path) / "knowledge-seed" / "protocols.md").exists()
        assert any("knowledge chunk" in n for n in res.notes)

    def test_a_snapshot_without_a_seed_creates_no_knowledge_store(self, ws_root):
        res = apply_snapshot(_snapshot_no_plugins(), name="vera-plain", acknowledged=True, install=False)
        assert not (Path(res.path) / "knowledge-seed").exists()

    def test_a_hostile_knowledge_path_cannot_escape(self):
        data = _zip({"agent.snapshot.yaml": _manifest(plugins=[]), "knowledge/../../evil.md": "x"})
        with pytest.raises(SnapshotError, match="unsafe path"):
            inspect_snapshot(data)
