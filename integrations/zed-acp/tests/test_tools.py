from protoagent_acp import tools
from protoagent_acp.roots import RootMap


def roots():
    return RootMap({"protoAgent": "/repo"})


def test_kinds():
    assert tools.tool_kind("read_file") == "read"
    assert tools.tool_kind("search_files") == "search"
    assert tools.tool_kind("edit_file") == "edit"
    assert tools.tool_kind("run_command") == "execute"
    assert tools.tool_kind("some_plugin_tool") == "other"


def test_parse_args_json_and_truncated_preview():
    assert tools.parse_args('{"project": "p", "path": "a.py"}') == {"project": "p", "path": "a.py"}
    # The server caps the args preview at 800 chars: a write_file's content truncates the
    # JSON, but the scalar fields that precede it still come through.
    cut = '{"project": "protoAgent", "path": "docs/x.md", "content": "# Title\\nlong body that never clo'
    assert tools.parse_args(cut) == {"project": "protoAgent", "path": "docs/x.md"}
    assert tools.parse_args("") == {}
    assert tools.parse_args({"a": 1}) == {"a": 1}


def test_read_file_location_is_absolute_and_1_based():
    title, locs = tools.describe("read_file", {"project": "protoAgent", "path": "README.md", "offset": 5, "limit": 20}, roots())
    assert locs == [{"path": "/repo/README.md", "line": 5}]
    assert title == "Read protoAgent/README.md (lines 5–24)"


def test_unknown_project_or_escape_gives_no_location():
    assert tools.describe("read_file", {"project": "nope", "path": "a"}, roots())[1] == []
    assert tools.describe("read_file", {"project": "protoAgent", "path": "../../etc/passwd"}, roots())[1] == []


def test_single_root_resolves_without_project():
    assert tools.describe("read_file", {"path": "a.py"}, roots())[1] == [{"path": "/repo/a.py"}]


def test_search_hits_become_locations():
    out = "server/a2a.py:120: async def handle\nserver/a2a.py-121- ctx\n--\ngraph/x.py:9: y"
    locs = tools.result_locations("search_files", {"project": "protoAgent", "query": "q"}, out, roots())
    assert locs == [{"path": "/repo/server/a2a.py", "line": 120}, {"path": "/repo/graph/x.py", "line": 9}]


def test_list_projects_output_teaches_roots():
    r = RootMap()
    r.learn_from_list_projects("Managed projects:\n- protoAgent  [ro]  /Users/me/dev/pa\n- other  [rw/no-delete]  /x/y")
    assert r.roots == {"protoAgent": "/Users/me/dev/pa", "other": "/x/y"}
    assert r.project_for("/Users/me/dev/pa/sub") == ("protoAgent", "/Users/me/dev/pa")


def test_override_beats_discovery():
    r = RootMap({"p": "/local"})
    r.add("p", "/remote")
    assert r.roots["p"] == "/local"


def test_early_announce_without_args_gets_a_neutral_title():
    assert tools.describe("search_files", {}, roots()) == ("Search files", [])


async def test_load_unions_explicit_fence_and_registry():
    class Api:
        async def get_json(self, path):
            return {
                "/api/config": {"config": {"filesystem": {"projects": [{"name": "protoAgent", "path": "/team"}]}}},
                "/api/projects": {"projects": [{"name": "protoAgent", "path": "/elsewhere"}, {"name": "b", "path": "/b"}]},
            }.get(path)

    r = RootMap()
    await r.load(Api())
    assert r.roots == {"protoAgent": "/team", "b": "/b"}  # the explicit fence wins a name clash
