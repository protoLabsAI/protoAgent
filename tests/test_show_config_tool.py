"""The agent can read its own effective config — without reading its own secrets (#2540)."""

from __future__ import annotations

import json

import pytest

from graph.config import LangGraphConfig
from tools.config_tools import (
    _REDACTED,
    _escape_segment,
    _join_path,
    _split_path,
    build_config_tools,
    redact_for_agent,
)


@pytest.fixture
def show_config():
    cfg = LangGraphConfig()
    cfg.mcp_servers = [
        {
            "name": "gh",
            "transport": "stdio",
            "command": "gh-mcp",
            "env": {"GITHUB_TOKEN": "ghp_realtoken", "SOME_FLAG": "1"},
        },
        {
            "name": "remote",
            "transport": "http",
            "url": "https://mcp.example.com",
            "headers": {"Authorization": "Bearer sk-live-abc"},
        },
    ]
    cfg.plugin_config = {
        "project_board": {"repo": "protoLabsAI/protoAgent", "api_token": "pbt_live"}
    }
    return build_config_tools(cfg)[0]


@pytest.fixture
def show_config_with_projects():
    cfg = LangGraphConfig()
    cfg.plugin_config = {
        "project_board": {
            "repo": "protoLabsAI/protoAgent",
            "projects": {
                f"proj-{i:03d}": {
                    "repo": f"org/repo-{i:03d}",
                    "status": "active",
                    "api_token": f"pbt_project_{i:03d}",
                }
                for i in range(7)
            },
        }
    }
    return build_config_tools(cfg)[0]


def test_agent_can_see_how_its_plugin_is_bound(show_config):
    """The #2540 incident: two sessions lost to a board bound to the wrong repo, with
    the answer in a file no tool could open."""
    out = show_config.invoke({"section": "project_board"})

    assert "protoLabsAI/protoAgent" in out
    assert json.loads(out)["project_board"]["repo"] == "protoLabsAI/protoAgent"


def test_dotted_selector_reads_nested_value(show_config_with_projects):
    out = show_config_with_projects.invoke(
        {"section": "project_board.projects.proj-003"}
    )

    assert json.loads(out)["project_board.projects.proj-003"]["repo"] == "org/repo-003"


def test_exact_top_level_section_with_dot_still_wins_over_path_parsing():
    cfg = LangGraphConfig()
    cfg.plugin_config = {
        "vendor.plugin": {"repo": "org/exact-section"},
        "vendor": {"plugin": {"repo": "org/nested-section"}},
    }
    show_config = build_config_tools(cfg)[0]

    out = show_config.invoke({"section": "vendor.plugin"})

    assert json.loads(out)["vendor.plugin"]["repo"] == "org/exact-section"


def test_dotted_selector_reports_missing_path(show_config_with_projects):
    out = show_config_with_projects.invoke({"section": "project_board.projects.missing"})

    assert out.startswith("Error: no config path 'project_board.projects.missing'")
    assert "missing 'missing' at project_board.projects" in out
    assert "proj-000" in out


def test_nested_map_can_be_reconstructed_from_deterministic_pages(show_config_with_projects):
    first = json.loads(
        show_config_with_projects.invoke({"section": "project_board.projects", "limit": 3})
    )
    second = json.loads(
        show_config_with_projects.invoke(
            {
                "section": "project_board.projects",
                "offset": first["pagination"]["next_offset"],
                "limit": 3,
            }
        )
    )
    third = json.loads(
        show_config_with_projects.invoke(
            {
                "section": "project_board.projects",
                "offset": second["pagination"]["next_offset"],
                "limit": 3,
            }
        )
    )

    assert first["pagination"] == {
        "offset": 0,
        "limit": 3,
        "returned": 3,
        "total": 7,
        "next_offset": 3,
        "has_more": True,
    }
    assert second["pagination"]["next_offset"] == 6
    assert third["pagination"] == {
        "offset": 6,
        "limit": 3,
        "returned": 1,
        "total": 7,
        "next_offset": None,
        "has_more": False,
    }
    reconstructed = {}
    reconstructed.update(first["value"])
    reconstructed.update(second["value"])
    reconstructed.update(third["value"])
    assert list(reconstructed) == [f"proj-{i:03d}" for i in range(7)]
    assert reconstructed["proj-006"]["repo"] == "org/repo-006"


def test_large_nested_map_exceeding_cap_is_readable_across_pages():
    cfg = LangGraphConfig()
    cfg.plugin_config = {
        "project_board": {
            "projects": {
                f"proj-{i:03d}": {
                    "repo": f"org/repo-{i:03d}",
                    "description": "x" * 400,
                }
                for i in range(40)
            }
        }
    }
    show_config = build_config_tools(cfg)[0]

    whole = show_config.invoke({"section": "project_board.projects"})
    assert "truncated at" not in whole
    assert json.loads(whole)["pagination"]["total"] == 40

    reconstructed = {}
    offset = 0
    while True:
        page = json.loads(
            show_config.invoke(
                {"section": "project_board.projects", "offset": offset, "limit": 10}
            )
        )
        reconstructed.update(page["value"])
        if page["pagination"]["next_offset"] is None:
            break
        offset = page["pagination"]["next_offset"]

    assert len(reconstructed) == 40
    assert reconstructed["proj-000"]["description"] == "x" * 400
    assert reconstructed["proj-039"]["repo"] == "org/repo-039"


def test_parent_with_one_oversized_child_points_instead_of_dead_ending():
    """Selecting a parent whose single child busts the cap must not dead-end: the child
    comes back as a drill-in pointer, the cursor still advances, and the full child stays
    readable via the deeper dotted path — no data lost, nothing silently cut."""
    cfg = LangGraphConfig()
    cfg.plugin_config = {
        "project_board": {
            "repo": "org/repo",
            "projects": {
                f"proj-{i:03d}": {
                    "repo": f"org/repo-{i:03d}",
                    "api_token": f"pbt_project_{i:03d}",
                    "blob": "y" * 500,
                }
                for i in range(60)
            },
        }
    }
    show_config = build_config_tools(cfg)[0]

    first = show_config.invoke({"section": "project_board"})
    assert "truncated at" not in first  # not the old silent-cut sentinel
    page = json.loads(first)
    # `projects` sorts before `repo`, so it is the first (oversized) child.
    pointer = page["value"]["projects"]
    assert pointer["__truncated__"] is True
    assert pointer["read_with"] == "project_board.projects"
    assert pointer["type"] == "object"
    assert pointer["keys"] == 60
    # A pointer carries shape only — never the value, so no nested secret rides along.
    assert "pbt_project_" not in first
    # The cursor advanced by one, so the sibling `repo` is still reachable.
    assert page["pagination"]["returned"] == 1
    assert page["pagination"]["has_more"] is True

    # Following the pointer reconstructs the whole child across pages, redacted throughout.
    reconstructed = {}
    offset = 0
    while True:
        child_page = json.loads(
            show_config.invoke(
                {"section": "project_board.projects", "offset": offset, "limit": 10}
            )
        )
        reconstructed.update(child_page["value"])
        if child_page["pagination"]["next_offset"] is None:
            break
        offset = child_page["pagination"]["next_offset"]

    assert len(reconstructed) == 60
    assert reconstructed["proj-059"]["repo"] == "org/repo-059"
    assert reconstructed["proj-000"]["api_token"] == _REDACTED


@pytest.mark.parametrize(
    "segments",
    [
        ["project_board", "projects", "proj-003"],
        ["project_board", "registry", "scope.one"],  # dot inside a key
        ["vendor.plugin", "child"],  # dot in a top-level key name
        ["a", "", "b"],  # empty-string key in the middle
        ["parent", ""],  # empty-string key at the leaf
        ["with\\slash", "x"],  # backslash inside a key
        ["dot.and\\slash", ""],  # every special char at once
    ],
)
def test_path_escaping_round_trips_any_key(segments):
    """The invariant the drill-in pointer relies on: escaping then re-splitting a segment
    list returns it verbatim, so a pointer built from resolved segments always resolves
    back to the same child — even when a key holds a dot, a backslash, or is empty."""
    assert _split_path(_join_path(segments)) == segments
    for seg in segments:
        assert _split_path(_escape_segment(seg)) == [seg]


def test_oversized_child_with_dotted_key_stays_addressable():
    """Review fix: raw dot-concatenation produced an unresolvable pointer for a child whose
    key contains a dot, so its value was silently lost. The escaped selector resolves
    straight back to that child, and the whole child reconstructs across pages."""
    child = {f"item-{i:03d}": "z" * 20 for i in range(500)}
    child["api_token"] = "pbt_secret"
    cfg = LangGraphConfig()
    cfg.plugin_config = {"project_board": {"registry": {"scope.one": child}}}
    show_config = build_config_tools(cfg)[0]

    page = json.loads(show_config.invoke({"section": "project_board.registry"}))
    pointer = page["value"]["scope.one"]
    assert pointer["__truncated__"] is True
    assert pointer["type"] == "object"
    # The dot in the key is escaped, so the deeper path targets the child itself rather
    # than splitting into project_board.registry.scope -> one.
    assert pointer["read_with"] == r"project_board.registry.scope\.one"
    assert "pbt_secret" not in json.dumps(page)  # pointer carries shape only, never value

    reconstructed = {}
    offset = 0
    while True:
        cp = json.loads(
            show_config.invoke(
                {"section": pointer["read_with"], "offset": offset, "limit": 100}
            )
        )
        reconstructed.update(cp["value"])
        if cp["pagination"]["next_offset"] is None:
            break
        offset = cp["pagination"]["next_offset"]

    assert len(reconstructed) == 501
    assert reconstructed["item-499"] == "z" * 20
    assert reconstructed["api_token"] == _REDACTED  # redacted at depth, through the pointer


def test_oversized_child_with_empty_key_stays_addressable():
    """Review fix: an oversized child under an empty-string key must stay pageable too. Its
    pointer resolves through a trailing empty segment instead of dead-ending."""
    child = {f"item-{i:03d}": "z" * 20 for i in range(500)}
    cfg = LangGraphConfig()
    cfg.plugin_config = {"project_board": {"registry": {"": child}}}
    show_config = build_config_tools(cfg)[0]

    page = json.loads(show_config.invoke({"section": "project_board.registry"}))
    pointer = page["value"][""]
    assert pointer["__truncated__"] is True
    assert pointer["read_with"] == "project_board.registry."

    reconstructed = {}
    offset = 0
    while True:
        cp = json.loads(
            show_config.invoke(
                {"section": pointer["read_with"], "offset": offset, "limit": 100}
            )
        )
        reconstructed.update(cp["value"])
        if cp["pagination"]["next_offset"] is None:
            break
        offset = cp["pagination"]["next_offset"]

    assert len(reconstructed) == 500
    assert reconstructed["item-000"] == "z" * 20


def test_dotted_key_child_reads_directly_via_escaped_selector():
    """A nested key containing a dot is reachable head-on with an escaped selector, not
    only via a drill-in pointer."""
    cfg = LangGraphConfig()
    cfg.plugin_config = {
        "project_board": {"registry": {"scope.one": {"repo": "org/dotted"}}}
    }
    show_config = build_config_tools(cfg)[0]

    out = show_config.invoke({"section": r"project_board.registry.scope\.one"})

    assert json.loads(out)[r"project_board.registry.scope\.one"]["repo"] == "org/dotted"


def test_large_selected_string_is_paged_without_truncated_json():
    large_note = "abcdefghijklmnopqrstuvwxyz" * 600
    cfg = LangGraphConfig()
    cfg.plugin_config = {
        "project_board": {
            "large_note": large_note,
        }
    }
    show_config = build_config_tools(cfg)[0]

    first = json.loads(show_config.invoke({"section": "project_board.large_note"}))

    assert first["pagination"] == {
        "offset": 0,
        "limit": 100,
        "returned": 100,
        "total": 15600,
        "next_offset": 100,
        "has_more": True,
    }

    chunks = [first["value"]]
    offset = first["pagination"]["next_offset"]
    while offset is not None:
        page = json.loads(
            show_config.invoke(
                {
                    "section": "project_board.large_note",
                    "offset": offset,
                    "limit": 500,
                }
            )
        )
        chunks.append(page["value"])
        offset = page["pagination"]["next_offset"]

    assert "".join(chunks) == large_note


def test_nested_page_redacts_secret_shaped_values(show_config_with_projects):
    out = show_config_with_projects.invoke(
        {"section": "project_board.projects", "limit": 2}
    )

    assert "pbt_project_" not in out
    page = json.loads(out)
    assert page["value"]["proj-000"]["api_token"] == _REDACTED
    assert page["value"]["proj-001"]["api_token"] == _REDACTED


def test_mcp_env_and_headers_never_reach_the_model(show_config):
    """`config_to_dict` emits these verbatim — correct for the token-gated operator API,
    wrong for a destination that is the model's context and the chat transcript."""
    out = show_config.invoke({"section": "mcp"})

    assert "ghp_realtoken" not in out
    assert "sk-live-abc" not in out
    servers = {s["name"]: s for s in json.loads(out)["mcp"]["servers"]}
    # Masked, not dropped — "a token IS set here" is exactly what a diagnosing agent needs.
    assert servers["gh"]["env"]["GITHUB_TOKEN"] == _REDACTED
    assert servers["gh"]["env"]["SOME_FLAG"] == _REDACTED
    assert servers["remote"]["headers"]["Authorization"] == _REDACTED
    # Non-secret wiring survives, or the tool would be useless for diagnosis.
    assert servers["gh"]["command"] == "gh-mcp"
    assert servers["remote"]["url"] == "https://mcp.example.com"


def test_no_secret_survives_anywhere_in_the_whole_document(show_config):
    """The blanket check: whatever section it sits in, a credential must not come back."""
    out = show_config.invoke({})

    for leaked in ("ghp_realtoken", "sk-live-abc", "pbt_live"):
        assert leaked not in out, leaked


def test_redaction_covers_sections_nobody_updated_this_module_for():
    """Fails closed by SHAPE, so a plugin section added later is covered without anyone
    remembering to come back here."""
    doc = redact_for_agent(
        {
            "some_future_plugin": {
                "endpoint": "https://api.example.com",
                "access_token": "tok_live_123",
                "nested": [{"client_secret": "cs_live_456"}],
                "env": {"ANYTHING": "could-be-a-token"},
            }
        }
    )

    section = doc["some_future_plugin"]
    assert section["endpoint"] == "https://api.example.com"
    assert section["access_token"] == _REDACTED
    assert section["nested"][0]["client_secret"] == _REDACTED
    assert section["env"]["ANYTHING"] == _REDACTED


def test_blank_values_are_left_alone_so_unset_still_reads_as_unset():
    """Masking a blank would say "a token is set here" when none is — the exact
    confusion this tool exists to end."""
    doc = redact_for_agent({"svc": {"api_key": "", "token": None, "env": {"EMPTY": ""}}})

    assert doc["svc"]["api_key"] == ""
    assert doc["svc"]["token"] is None
    assert doc["svc"]["env"]["EMPTY"] == ""


def test_unknown_section_suggests_instead_of_failing_blind(show_config):
    out = show_config.invoke({"section": "project_bord"})

    assert out.startswith("Error:")
    assert "project_board" in out  # near-match suggestion
    assert "Sections:" in out


def test_tool_description_tells_the_model_what_the_marker_means(show_config):
    """A masked value is only useful if the model knows a mask is what it's seeing."""
    assert _REDACTED in show_config.description
    assert "Read-only" in show_config.description


@pytest.fixture
def serving(monkeypatch):
    """Pretend a server is actually serving in THIS process: ``bound_base_url`` reads the
    bind host + port straight off ``STATE`` (never a socket). ``main_loop`` is the "a loop
    is up" signal it gates on. monkeypatch restores the singleton after the test."""
    from runtime.state import STATE

    def _bind(host, port):
        monkeypatch.setattr(STATE, "main_loop", object())
        monkeypatch.setattr(STATE, "bind_host", host)
        monkeypatch.setattr(STATE, "active_port", port)

    return _bind


@pytest.fixture
def not_serving(monkeypatch):
    """No server serving — pin ``main_loop`` to None so the "unknown port" path is
    deterministic even if another test left the process-wide singleton populated."""
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "main_loop", None)
    return STATE


def test_self_url_section_reports_local_public_preferred(serving):
    serving("127.0.0.1", 7871)
    cfg = LangGraphConfig()
    cfg.public_console_url = "https://agent.example.com"
    tool = build_config_tools(cfg)[0]

    out = json.loads(tool.invoke({"section": "self_url"}))["self_url"]

    assert out["local"] == "http://127.0.0.1:7871"
    assert out["public"] == "https://agent.example.com"
    assert out["preferred"] == "https://agent.example.com"
    assert "note" not in out


def test_self_url_preferred_falls_back_to_local_without_public(serving):
    serving("127.0.0.1", 7871)
    tool = build_config_tools(LangGraphConfig())[0]  # public_console_url defaults to ""

    out = json.loads(tool.invoke({"section": "self_url"}))["self_url"]

    assert out["public"] == ""
    assert out["local"] == "http://127.0.0.1:7871"
    assert out["preferred"] == "http://127.0.0.1:7871"


def test_self_url_wildcard_bind_renders_as_loopback(serving):
    """A 0.0.0.0 / :: bind listens on loopback too, so a same-host client reaches it at
    127.0.0.1 — never report the wildcard as the address."""
    serving("0.0.0.0", 7871)
    tool = build_config_tools(LangGraphConfig())[0]

    out = json.loads(tool.invoke({"section": "self_url"}))["self_url"]

    assert out["local"] == "http://127.0.0.1:7871"
    assert out["preferred"] == "http://127.0.0.1:7871"


def test_self_url_without_running_server_is_blank_with_a_note(not_serving):
    """Tools built outside a running server can't know the bound port — report "" and say
    why, and never guess or probe."""
    cfg = LangGraphConfig()
    cfg.public_console_url = "https://agent.example.com"
    tool = build_config_tools(cfg)[0]

    out = json.loads(tool.invoke({"section": "self_url"}))["self_url"]

    assert out["local"] == ""
    assert out["note"]
    # A public URL is still known even with no local server, and is still preferred.
    assert out["public"] == "https://agent.example.com"
    assert out["preferred"] == "https://agent.example.com"


def test_self_url_without_server_or_public_is_entirely_blank(not_serving):
    tool = build_config_tools(LangGraphConfig())[0]

    out = json.loads(tool.invoke({"section": "self_url"}))["self_url"]

    assert out == {"local": "", "public": "", "preferred": "", "note": out["note"]}
    assert out["note"]


def test_self_url_appears_in_whole_config_view(not_serving):
    """It rides the whole-config view (and the section list), not only an explicit
    ``section="self_url"`` selection."""
    out = build_config_tools(LangGraphConfig())[0].invoke({})
    doc = json.loads(out)

    assert "self_url" in doc
    assert set(doc["self_url"]) >= {"local", "public", "preferred"}


def test_self_url_listed_in_section_index_when_config_too_large(not_serving):
    """When the whole config is too big to render, the fallback is a section index — and
    ``self_url`` must still be listed there as a selectable section."""
    cfg = LangGraphConfig()
    cfg.plugin_config = {"bulky": {f"k{i:03d}": "x" * 400 for i in range(100)}}
    out = build_config_tools(cfg)[0].invoke({})

    assert "too large to show at once" in out  # the section-index fallback, not full JSON
    assert "self_url" in out


def test_self_url_opens_no_socket(serving, monkeypatch):
    """The whole point is to NOT probe ports: resolving self_url must never touch a socket."""
    import socket

    def _no_sockets(*a, **k):
        raise AssertionError("show_config(self_url) must not open a socket")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    serving("0.0.0.0", 7871)
    tool = build_config_tools(LangGraphConfig())[0]

    out = json.loads(tool.invoke({"section": "self_url"}))["self_url"]

    assert out["local"] == "http://127.0.0.1:7871"


def test_self_url_documented_as_the_port_probing_alternative():
    tool = build_config_tools(LangGraphConfig())[0]

    assert "self_url" in tool.description
    assert "probing ports" in tool.description


def test_show_config_binds_with_the_core_toolset():
    from tools.lg_tools import get_all_tools

    names = {t.name for t in get_all_tools(graph_config=LangGraphConfig())}
    assert "show_config" in names

    # No config → nothing to introspect, and nothing to leak.
    assert "show_config" not in {t.name for t in get_all_tools()}


def test_operator_can_turn_it_off():
    """ADR 0005: the operator denylist covers it like any other core tool."""
    from tools.lg_tools import get_all_tools, set_disabled_tools

    try:
        set_disabled_tools(["show_config"])
        names = {t.name for t in get_all_tools(graph_config=LangGraphConfig())}
        assert "show_config" not in names
    finally:
        set_disabled_tools([])


@pytest.mark.parametrize("value", [
    ["synthetic-secret"],
    [["synthetic-secret"]],
    [{"value": "synthetic-secret"}],
    {"primary": "synthetic-secret", "nested": ["synthetic-secret"]},
])
def test_sensitive_container_is_masked_without_mutating_config(value):
    import copy

    original = {"plugin": {"API_KEYS": value, "endpoint": "https://example.com"}}
    before = copy.deepcopy(original)
    redacted = redact_for_agent(original)
    assert redacted["plugin"]["API_KEYS"] == _REDACTED
    assert "synthetic-secret" not in json.dumps(redacted)
    assert redacted["plugin"]["endpoint"] == "https://example.com"
    assert original == before


@pytest.mark.parametrize("empty", [None, "", [], {}])
def test_unset_sensitive_containers_remain_unset(empty):
    assert redact_for_agent({"credentials": empty}) == {"credentials": empty}


def test_show_config_masks_secret_containers_before_selection_and_pagination():
    cfg = LangGraphConfig()
    cfg.plugin_config = {"future_plugin": {
        "api_keys": ["synthetic-secret"],
        "credentials": {"primary": "synthetic-secret"},
        "endpoint": "https://example.com",
    }}
    tool = build_config_tools(cfg)[0]
    for args in ({}, {"section": "future_plugin"},
                 {"section": "future_plugin", "limit": 1},
                 {"section": "future_plugin.api_keys"},
                 {"section": "future_plugin.api_keys.0"},
                 {"section": "future_plugin.credentials.primary"}):
        result = tool.invoke(args)
        assert "synthetic-secret" not in result
    assert json.loads(tool.invoke({"section": "future_plugin.api_keys"})) == {
        "future_plugin.api_keys": _REDACTED
    }
