"""Agent snapshot export (ADR 0091 D1/D2, #2103).

The bar this file defends is ADR 0091's acceptance bar, stated as a test: **the exported
artifact could be pushed to a public gist without leaking a single credential.** So the
centrepiece is a grep-the-artifact litmus — build a snapshot from an agent configured with
a gateway key, an MCP server carrying an env token, a plugin secret and a pasted token, then
assert none of those strings appear ANYWHERE in the zip's bytes.

The rest of the file covers the two failure modes that make a leak likely: a credential the
declared-key strip cannot see (free text, MCP env), and the export path quietly inheriting
the SAVE path's fail-open behavior.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from io import BytesIO

import pytest
import yaml

from graph.snapshot_op import (
    SNAPSHOT_MANIFEST,
    build_snapshot,
    redact_config_for_export,
)

# Distinctive values that must never survive an export. Each is shaped like the real thing
# so the PATTERN layer is genuinely exercised, not just the structural one.
#
# Assembled at runtime rather than written as literals: a real-looking credential committed
# to the tree trips the repo's gitleaks gate (it caught this file's AWS canary on the first
# CI run). The alternative — allowlisting this path — would keep the literals readable but
# blind the gate to a genuine secret accidentally added HERE later, which is a bad trade in
# the one file whose whole job is proving secrets don't escape.
_C = "LEAKCANARY"
GATEWAY_KEY = "sk-" + "proj-" + _C + "0" * 24
MCP_TOKEN = "ghp_" + _C + "1" * 30
PLUGIN_SECRET = "xoxb-" + _C + "-2222222222-3333333333"
PASTED_IN_SOUL = "sk-" + "ant-" + _C + "4" * 26
PASTED_IN_FIELD = "AKIA" + _C + "5555XY"  # AKIA + exactly 16 upper/digit chars
ALL_CANARIES = (GATEWAY_KEY, MCP_TOKEN, PLUGIN_SECRET, PASTED_IN_SOUL, PASTED_IN_FIELD)

SECRET_KEYS = (("model", "api_key"), ("auth", "token"), ("discord", "bot_token"))


def _config() -> dict:
    return {
        "model": {"name": "protolabs/reasoning", "api_key": GATEWAY_KEY},
        "auth": {"token": "operator-bearer-token"},
        "discord": {"bot_token": PLUGIN_SECRET, "guild": "protolabs"},
        "mcp": {
            "servers": [
                {
                    "name": "github",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": MCP_TOKEN},
                },
                {
                    "name": "vendor",
                    "transport": "http",
                    "url": "https://vendor.example/mcp",
                    "headers": {"Authorization": f"Bearer {MCP_TOKEN}"},
                },
            ]
        },
        # A credential nobody declared, sitting in an ordinary text field — invisible to
        # the declared-key strip, which is exactly why the pattern layer exists.
        "notes": {"scratch": f"remember to rotate {PASTED_IN_FIELD} next month"},
        "agent": {"name": "vera"},
    }


@pytest.fixture
def agent_tree(tmp_path):
    """A configured instance root: config, SOUL, plugins.lock, a skill, and the two files
    that must never travel (secrets.yaml, .fleet-token)."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "langgraph-config.yaml").write_text(yaml.safe_dump(_config()), encoding="utf-8")
    (cfg / "SOUL.md").write_text(
        f"# Vera\n\nYou review PRs.\n\nLegacy note: the old key was {PASTED_IN_SOUL}, do not use it.\n",
        encoding="utf-8",
    )
    (cfg / "secrets.yaml").write_text(yaml.safe_dump({"model": {"api_key": GATEWAY_KEY}}), encoding="utf-8")
    (tmp_path / ".fleet-token").write_text("fleet-service-token-value", encoding="utf-8")
    (tmp_path / "plugins.lock").write_text(
        json.dumps(
            {"plugins": [{"id": "github", "url": "https://github.com/protoLabsAI/github-plugin", "sha": "abc123"}]}
        ),
        encoding="utf-8",
    )
    skills = tmp_path / "skills" / "reviewing"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# Reviewing\n\nCheck the diff.\n", encoding="utf-8")
    return tmp_path


def _build(tree, **kw):
    return build_snapshot(
        config_yaml=tree / "config" / "langgraph-config.yaml",
        soul_path=tree / "config" / "SOUL.md",
        plugins_lock=tree / "plugins.lock",
        skills_dirs={"instance": tree / "skills"},
        agent_name="vera",
        secret_key_paths=SECRET_KEYS,
        plugin_requirements=[],  # keep the fixture off the dev box's installed plugins
        now=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        **kw,
    )


# ── the 12-factor litmus ─────────────────────────────────────────────────────────────


class TestPublicGistLitmus:
    def test_no_credential_survives_anywhere_in_the_artifact(self, agent_tree):
        """ADR 0091's acceptance bar, as a test. Greps the RAW ZIP BYTES and every member's
        decompressed text — not just the manifest — so a credential can't hide in a skill
        file, a filename, or an unexamined blob."""
        result = _build(agent_tree)

        for canary in ALL_CANARIES:
            assert canary.encode() not in result.data, f"{canary[:16]}… survived in the raw zip bytes"

        with zipfile.ZipFile(BytesIO(result.data)) as zf:
            for name in zf.namelist():
                body = zf.read(name).decode("utf-8", errors="replace")
                for canary in ALL_CANARIES:
                    assert canary not in body, f"{canary[:16]}… survived in {name}"

    def test_credential_bearing_files_are_never_members(self, agent_tree):
        """secrets.yaml / .fleet-token exist only to hold credentials — no redaction makes
        them portable, so they must not be in the zip at all."""
        result = _build(agent_tree)
        with zipfile.ZipFile(BytesIO(result.data)) as zf:
            names = zf.namelist()
        assert not any("secrets.yaml" in n for n in names)
        assert not any(".fleet-token" in n for n in names)

    def test_the_definition_actually_survives(self, agent_tree):
        """A snapshot that redacted everything would pass the litmus and be useless. The
        non-secret configuration, the persona, the pins and the skills must all travel."""
        result = _build(agent_tree)
        with zipfile.ZipFile(BytesIO(result.data)) as zf:
            manifest = yaml.safe_load(zf.read(SNAPSHOT_MANIFEST))
            soul = zf.read("SOUL.md").decode()
            skill = zf.read("skills/instance/reviewing/SKILL.md").decode()

        assert manifest["config"]["model"]["name"] == "protolabs/reasoning"
        assert manifest["config"]["discord"]["guild"] == "protolabs"
        assert manifest["config"]["agent"]["name"] == "vera"
        assert manifest["plugins"][0]["sha"] == "abc123"
        assert "You review PRs." in soul
        assert "Check the diff." in skill
        # MCP servers keep their SHAPE — the target needs to know which server wants which
        # var; only the values are nulled.
        github = next(s for s in manifest["config"]["mcp"]["servers"] if s["name"] == "github")
        assert github["command"] == "npx"
        assert github["env"] == {"GITHUB_TOKEN": ""}


# ── required_secrets ─────────────────────────────────────────────────────────────────


class TestRequiredSecrets:
    def test_every_stripped_credential_is_listed_by_name(self, agent_tree):
        result = _build(agent_tree)
        names = {r.name for r in result.required_secrets}
        assert "model.api_key" in names
        assert "auth.token" in names
        assert "discord.bot_token" in names
        assert "mcp.github.env.GITHUB_TOKEN" in names
        assert "mcp.vendor.headers.Authorization" in names

    def test_no_requirement_carries_a_value(self, agent_tree):
        """The whole point of the schema: names and descriptions, never values."""
        result = _build(agent_tree)
        blob = json.dumps([r.as_dict() for r in result.required_secrets])
        for canary in ALL_CANARIES:
            assert canary not in blob

    def test_was_set_distinguishes_configured_from_merely_declared(self, agent_tree):
        result = _build(agent_tree)
        by_name = {r.name: r for r in result.required_secrets}
        assert by_name["model.api_key"].was_set is True
        assert by_name["mcp.github.env.GITHUB_TOKEN"].was_set is True

    def test_a_credential_stored_ONLY_in_the_overlay_is_still_inventoried(self, tmp_path):
        """The regression that mattered: a correctly-configured agent keeps `model.api_key`
        in secrets.yaml and NOT inline, so an inline-only walk emitted no requirement for it
        at all — the gateway key the agent cannot run without was missing from the inventory,
        and import would stand up a dead agent without ever asking."""
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "langgraph-config.yaml").write_text(
            yaml.safe_dump({"model": {"name": "protolabs/reasoning"}}), encoding="utf-8"
        )
        (cfg / "secrets.yaml").write_text(yaml.safe_dump({"model": {"api_key": GATEWAY_KEY}}), encoding="utf-8")
        result = build_snapshot(
            config_yaml=cfg / "langgraph-config.yaml",
            soul_path=cfg / "SOUL.md",
            plugins_lock=tmp_path / "plugins.lock",
            secrets_yaml=cfg / "secrets.yaml",
            agent_name="stored",
            secret_key_paths=SECRET_KEYS,
            plugin_requirements=[],
        )
        by_name = {r.name: r for r in result.required_secrets}
        assert "model.api_key" in by_name, "a credential stored in secrets.yaml was not inventoried"
        assert by_name["model.api_key"].was_set is True
        assert GATEWAY_KEY.encode() not in result.data
        # the non-secret part of the section still travels
        assert result.manifest["config"]["model"]["name"] == "protolabs/reasoning"

    def test_a_secret_stored_in_the_overlay_counts_as_set(self, agent_tree):
        """A CORRECTLY configured agent keeps credentials in secrets.yaml, not inline. Reading
        only the inline config would report every one of them "declared, unset" — inverting
        the signal and telling the operator their working agent needs nothing."""
        result = _build(agent_tree, secrets_yaml=agent_tree / "config" / "secrets.yaml")
        by_name = {r.name: r for r in result.required_secrets}
        assert by_name["model.api_key"].was_set is True

    def test_overlay_only_ever_yields_a_boolean(self, agent_tree):
        """Reading secrets.yaml to answer `was_set` must not pull a VALUE into the artifact."""
        result = _build(agent_tree, secrets_yaml=agent_tree / "config" / "secrets.yaml")
        for canary in ALL_CANARIES:
            assert canary.encode() not in result.data

    def test_an_unset_declared_key_is_still_listed(self):
        """A plugin secret the operator never filled in must still be surfaced — import
        should ask, rather than stand up an agent whose plugin silently can't auth."""
        clean, reqs, _ = redact_config_for_export(
            {"discord": {"bot_token": "", "guild": "x"}}, secret_key_paths=SECRET_KEYS
        )
        by_name = {r.name: r for r in reqs}
        assert "discord.bot_token" in by_name
        assert by_name["discord.bot_token"].was_set is False
        assert clean["discord"] == {"guild": "x"}


# ── the redactor itself ──────────────────────────────────────────────────────────────


class TestRedactConfigForExport:
    def test_is_pure_and_does_not_mutate_the_input(self):
        """Export runs against the LIVE agent's config. Mutating it — as the save path's
        strip_secrets_from_doc does — would corrupt a running instance."""
        doc = _config()
        before = json.dumps(doc, sort_keys=True)
        redact_config_for_export(doc, secret_key_paths=SECRET_KEYS)
        assert json.dumps(doc, sort_keys=True) == before

    def test_fails_closed_dropping_the_value_rather_than_keeping_it(self):
        """The inversion that matters. `strip_secrets_from_doc` leaves a secret INLINE when
        it can't relocate it (#1645) — right for a file staying on the box, catastrophic for
        an artifact leaving it. Here a declared key is always gone, with nowhere it could be
        preserved to."""
        clean, _, _ = redact_config_for_export({"model": {"api_key": GATEWAY_KEY}}, secret_key_paths=SECRET_KEYS)
        assert "model" not in clean or "api_key" not in clean.get("model", {})
        assert GATEWAY_KEY not in json.dumps(clean)

    def test_empties_the_section_when_it_held_only_a_secret(self):
        clean, _, _ = redact_config_for_export({"auth": {"token": "x"}}, secret_key_paths=SECRET_KEYS)
        assert "auth" not in clean

    def test_pattern_layer_catches_an_undeclared_credential_in_free_text(self):
        """The case layer 1 structurally cannot see: a token in a field nobody declared."""
        clean, _, hits = redact_config_for_export(
            {"notes": {"scratch": f"key is {PASTED_IN_FIELD}"}}, secret_key_paths=SECRET_KEYS
        )
        assert PASTED_IN_FIELD not in json.dumps(clean)
        assert "aws-access-key" in hits.get("notes.scratch", [])

    def test_pattern_hits_are_reported_for_operator_review(self, agent_tree):
        """A silent filter is not reviewable. Anything the pattern layer matched is
        reported with WHERE it was found, so the operator can go fix the source."""
        result = _build(agent_tree)
        assert "SOUL.md" in result.pattern_redactions
        assert "anthropic-key" in result.pattern_redactions["SOUL.md"]

    def test_secrets_manager_section_is_dropped_wholesale(self):
        clean, _, _ = redact_config_for_export(
            {"secrets_manager": {"client_id": "a", "client_secret": "b", "url": "https://vault"}},
            secret_key_paths=SECRET_KEYS,
        )
        assert "secrets_manager" not in clean

    def test_survives_a_config_with_no_secrets_at_all(self):
        clean, reqs, hits = redact_config_for_export({"agent": {"name": "x"}}, secret_key_paths=SECRET_KEYS)
        assert clean == {"agent": {"name": "x"}}
        assert reqs == [] and hits == {}

    def test_tolerates_a_malformed_mcp_block(self):
        """Config is operator-editable; a hand-broken mcp block must not crash an export."""
        clean, reqs, _ = redact_config_for_export(
            {"mcp": {"servers": ["not-a-dict", {"name": "ok", "env": {"K": "v"}}]}},
            secret_key_paths=SECRET_KEYS,
        )
        assert clean["mcp"]["servers"][1]["env"] == {"K": ""}
        assert any(r.name == "mcp.ok.env.K" for r in reqs)


# ── manifest + packaging ─────────────────────────────────────────────────────────────


class TestManifest:
    def test_carries_plugin_pins_not_plugin_code(self, agent_tree):
        """Pins are what make the artifact small, auditable and reproducible (ADR 0091 D1);
        vendored code would make it none of those."""
        result = _build(agent_tree)
        with zipfile.ZipFile(BytesIO(result.data)) as zf:
            names = zf.namelist()
        assert not any(n.startswith("plugins/") for n in names)
        assert result.manifest["plugins"][0]["url"].endswith("github-plugin")

    def test_states_what_it_excludes(self, agent_tree):
        """A reader must be able to tell what this artifact is NOT — a snapshot yields a
        FRESH agent, not a resumed one (ADR 0091 D4)."""
        result = _build(agent_tree)
        assert "runtime_state" in result.manifest["excludes"]
        assert "knowledge" in result.manifest["excludes"]

    def test_filename_is_stable_and_safe(self, agent_tree):
        result = _build(agent_tree)
        assert result.filename == "vera-snapshot-20260803-120000.zip"

    def test_a_hostile_agent_name_cannot_escape_the_filename(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "langgraph-config.yaml").write_text("agent: {name: x}\n", encoding="utf-8")
        result = build_snapshot(
            config_yaml=tmp_path / "config" / "langgraph-config.yaml",
            soul_path=tmp_path / "config" / "SOUL.md",
            plugins_lock=tmp_path / "plugins.lock",
            agent_name="../../etc/passwd",
            secret_key_paths=SECRET_KEYS,
            plugin_requirements=[],
            now=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        )
        assert "/" not in result.filename and ".." not in result.filename

    def test_empty_agent_produces_a_valid_snapshot_with_notes(self, tmp_path):
        """A fresh install has no config and no SOUL — export must degrade to a valid,
        empty-but-honest artifact rather than raising."""
        result = build_snapshot(
            config_yaml=tmp_path / "missing.yaml",
            soul_path=tmp_path / "missing.md",
            plugins_lock=tmp_path / "missing.lock",
            secret_key_paths=SECRET_KEYS,
            plugin_requirements=[],
        )
        with zipfile.ZipFile(BytesIO(result.data)) as zf:
            assert SNAPSHOT_MANIFEST in zf.namelist()
        assert any("SOUL" in n for n in result.notes)

    def test_summary_is_json_safe_for_the_route(self, agent_tree):
        json.dumps(_build(agent_tree).summary())


# ── REVIEW.md — the disclosure that travels WITH the artifact ────────────────────────


class TestReviewDocument:
    def test_review_is_inside_the_zip(self, agent_tree):
        """It lives in the artifact, not only in an API response, so it can never be
        separated from what it describes — the chat export's disclosure pattern (#2158)."""
        result = _build(agent_tree)
        with zipfile.ZipFile(BytesIO(result.data)) as zf:
            review = zf.read("REVIEW.md").decode()
        assert "Snapshot review — vera" in review

    def test_review_lists_the_credentials_without_their_values(self, agent_tree):
        result = _build(agent_tree)
        with zipfile.ZipFile(BytesIO(result.data)) as zf:
            review = zf.read("REVIEW.md").decode()
        assert "model.api_key" in review
        assert "mcp.github.env.GITHUB_TOKEN" in review
        for canary in ALL_CANARIES:
            assert canary not in review

    def test_review_flags_credential_findings_as_exposed(self, agent_tree):
        """A credential pattern hit means the secret is still sitting in the SOURCE agent —
        the artifact is clean, the source is not. Say so, and say to rotate it."""
        result = _build(agent_tree)
        with zipfile.ZipFile(BytesIO(result.data)) as zf:
            review = zf.read("REVIEW.md").decode()
        assert "SOUL.md" in review
        assert "rotate it" in review

    def test_review_separates_machine_paths_from_credentials(self, tmp_path):
        """A scrubbed home path is NOT a breach — there is nothing to rotate, it just has
        to be re-pointed on the target. Filing it under the credential heading would send
        an operator hunting for an exposure that never happened."""
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "langgraph-config.yaml").write_text(
            yaml.safe_dump({"operator": {"project_dir": "/Users/jsmith/dev/thing"}}), encoding="utf-8"
        )
        result = build_snapshot(
            config_yaml=cfg / "langgraph-config.yaml",
            soul_path=cfg / "SOUL.md",
            plugins_lock=tmp_path / "plugins.lock",
            agent_name="pathy",
            secret_key_paths=SECRET_KEYS,
            plugin_requirements=[],
        )
        with zipfile.ZipFile(BytesIO(result.data)) as zf:
            review = zf.read("REVIEW.md").decode()
        credentials_section = review.split("## Machine-local")[0]
        assert "None — nothing credential-shaped was found" in credentials_section
        assert "re-point" in review
        assert "operator.project_dir" in review.split("## Machine-local")[1]

    def test_review_states_the_guarantee_is_not_absolute(self, agent_tree):
        """Over-claiming here would be the actual danger: an operator who believes the
        filter is exhaustive stops reading the artifact."""
        result = _build(agent_tree)
        with zipfile.ZipFile(BytesIO(result.data)) as zf:
            review = zf.read("REVIEW.md").decode()
        assert "not a guarantee" in review

    def test_clean_agent_review_says_so_plainly(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "langgraph-config.yaml").write_text("agent: {name: clean}\n", encoding="utf-8")
        result = build_snapshot(
            config_yaml=tmp_path / "config" / "langgraph-config.yaml",
            soul_path=tmp_path / "config" / "SOUL.md",
            plugins_lock=tmp_path / "plugins.lock",
            agent_name="clean",
            secret_key_paths=SECRET_KEYS,
            plugin_requirements=[],
        )
        with zipfile.ZipFile(BytesIO(result.data)) as zf:
            review = zf.read("REVIEW.md").decode()
        assert "None — this agent had no configured credentials." in review
        assert "None — nothing credential-shaped was found in free text." in review
