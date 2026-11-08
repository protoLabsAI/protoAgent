"""Static plugin checks operate on source, including plugins that cannot import."""

import json
import textwrap

import pytest

from graph.plugins.check import check_plugin
from graph.plugins.cli import run_plugin_cli


@pytest.fixture
def plugin(tmp_path):
    def make(source="", manifest="", files=None):
        (tmp_path / "protoagent.plugin.yaml").write_text("id: example\nname: Example\n" + manifest)
        (tmp_path / "plugin.py").write_text(textwrap.dedent(source))
        for name, content in (files or {}).items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return tmp_path

    return make


@pytest.mark.parametrize(
    "method,kind,topic", [("emit", "emits", "example.changed"), ("on", "subscribes", "other.changed")]
)
def test_undeclared_topic(plugin, method, kind, topic):
    root = plugin(f'def register(registry):\n    registry.{method}(topic="{topic}")\n')
    findings = check_plugin(root)
    assert [f.code for f in findings] == [f"{kind}-undeclared"]
    assert findings[0].severity == "warn"
    assert findings[0].line == 2
    assert topic in findings[0].fix


@pytest.mark.parametrize("kind", ["emits", "subscribes"])
def test_unused_topic(plugin, kind):
    findings = check_plugin(plugin(manifest=f"{kind}: [example.unused]\n"))
    assert [f.code for f in findings] == [f"{kind}-unused"]


def test_missing_public_path(plugin):
    findings = check_plugin(plugin(manifest="public_paths: [/plugins/example/missing]\n"))
    assert [(f.code, f.severity) for f in findings] == [("public-path-missing", "error")]


@pytest.mark.parametrize("filename", ["views/page.js", "page.html"])
@pytest.mark.parametrize(
    "message",
    [
        '{type: "protoagent:publish", topic: "changed", data: {}}',
        '{"topic": "example.changed", "type": "protoagent:publish", "data": {}}',
    ],
)
def test_page_publish(plugin, filename, message):
    findings = check_plugin(plugin(files={filename: f"parent.postMessage({message}, '*');"}))
    assert [f.code for f in findings] == ["page-publish-undeclared"]
    assert findings[0].file == filename


def test_tools_without_skills(plugin):
    findings = check_plugin(plugin("def register(registry):\n    registry.register_tools([hello])\n"))
    assert [f.code for f in findings] == ["tools-without-skills"]


@pytest.mark.parametrize("annotation", ["Request", "WebSocket", "Response", "BackgroundTasks", "fastapi.Request"])
def test_string_route_param(plugin, annotation):
    findings = check_plugin(plugin(f'@router.get("/hello")\nasync def hello(request: "{annotation}"):\n    pass\n'))
    assert [f.code for f in findings] == ["route-string-annotation"]


def test_future_route_module(plugin):
    findings = check_plugin(
        plugin('from __future__ import annotations\n@router.get("/hello")\ndef hello(request: Request):\n    pass\n')
    )
    assert {f.code for f in findings} == {"route-future-annotations", "route-string-annotation"}


@pytest.mark.parametrize("param", ["config", "callbacks", "run_manager"])
@pytest.mark.parametrize("decorator", ["@tool", '@tool("hello")', ""])
def test_reserved_tool_params(plugin, param, decorator):
    root = plugin(
        f"{decorator}\ndef hello({param}: str):\n    pass\ndef register(registry):\n    registry.register_tool(hello)\n",
        files={"skills/hello/SKILL.md": "Use hello."},
    )
    findings = check_plugin(root)
    assert [f.code for f in findings] == ["tool-reserved-param"]
    assert param in findings[0].message


def test_clean_and_never_imported(plugin):
    root = plugin(
        """
        raise AssertionError("Plugin must never execute")
        from nonexistent_dependency import tool, APIRouter, Request
        router = APIRouter(prefix="/plugins/example")
        @router.get("/hello")
        def hello(request: Request):
            pass
        @tool
        def work(value: str, config: RunnableConfig, callbacks: Callbacks, run_manager: CallbackManagerForToolRun):
            pass
        def register(registry):
            registry.register_tool(work)
            registry.register_router(router, prefix="")
            registry.emit("changed")
            registry.on("other.changed", hello)
    """,
        "emits: [example.changed, example.page]\nsubscribes: [other.changed]\npublic_paths: [/plugins/example/hello, /plugins/example/views/index.html]\n",
        {
            "skills/work/SKILL.md": "Use work.",
            "views/index.html": '<script>parent.postMessage({type: "protoagent:publish", topic: "example.page"}, "*")</script>',
        },
    )
    assert check_plugin(root) == []


def test_unrelated_functions_and_dynamic_topics(plugin):
    root = plugin("""
        from __future__ import annotations
        def ordinary(config: str, request: "Request"):
            unrelated.emit("other")
            registry.emit(dynamic_topic)
    """)
    assert check_plugin(root) == []


@pytest.mark.parametrize(
    "manifest,code", [("emits: [example.unused]\n", 0), ("public_paths: [/plugins/example/missing]\n", 1), ("", 0)]
)
def test_cli_json_and_exit(plugin, capsys, manifest, code):
    root = plugin(manifest=manifest)
    assert run_plugin_cli(["check", str(root), "--json"]) == code
    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list)
    for row in rows:
        assert set(row) == {"severity", "code", "file", "line", "message", "fix"}
        assert isinstance(row["line"], int)
        assert row["fix"]
    assert run_plugin_cli(["check", str(root)]) == code
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(rows)
    assert all("Fix:" in line for line in lines)


def test_invalid_manifest(tmp_path, capsys):
    assert run_plugin_cli(["check", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)[0]["code"] == "manifest-invalid"


def test_invalid_python(plugin):
    findings = check_plugin(plugin("def broken("))
    assert [(f.code, f.severity) for f in findings] == [("python-syntax", "error")]


def test_ignores_test_and_dependency_sources(plugin):
    root = plugin(files={"tests/test_example.py": 'registry.emit("test")', ".venv/lib/broken.py": "def broken("})
    assert check_plugin(root) == []


@pytest.mark.parametrize(
    "mount,public",
    [
        ("registry.register_router(router)", "/plugins/example/hello"),
        ('registry.register_router(router, prefix="/api/plugins/example")', "/api/plugins/example/hello"),
    ],
)
def test_router_mount_prefix(plugin, mount, public):
    root = plugin(
        f'router = APIRouter()\n@router.get("/hello")\ndef hello():\n    pass\ndef register(registry):\n    {mount}\n',
        f"public_paths: [{public}]\n",
    )
    assert check_plugin(root) == []
