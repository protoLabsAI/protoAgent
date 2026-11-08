"""Host-free, best-effort static checks. Plugin source is parsed, never imported."""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from graph.plugins.manifest import MANIFEST_FILENAME, load_manifest
from graph.plugins.testkit import (
    _ASGI_INJECTED_TYPES,
    _TOOL_RESERVED_PARAMS,
    _annotation_names_type,
    _tool_param_annotation_is_injected,
)


@dataclass(frozen=True)
class Finding:
    """One actionable diagnostic; file is relative to the plugin, line is one-based."""

    severity: Literal["error", "warn"]
    code: str
    file: str
    line: int
    message: str
    fix: str


_ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "websocket", "api_route", "route"}
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "tests"}
_PAGE_OBJECT = re.compile(r"\{[^{}]*?['\"]?type['\"]?\s*:\s*['\"]protoagent:publish['\"][^{}]*", re.S)
_TOPIC = re.compile(r"\btopic['\"]?\s*:\s*(['\"])(.*?)\1", re.S)


def _string(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _argument(call, name, position=0):
    return next(
        (k.value for k in call.keywords if k.arg == name), call.args[position] if len(call.args) > position else None
    )


def _name(node) -> str:
    return ast.unparse(node) if node is not None else ""


def check_plugin(directory: str | Path) -> list[Finding]:
    """Check literal contracts and registration hazards without executing plugin code.

    Dynamic topics/paths and indirect registrations cannot be proven statically.
    Topic drift and authoring hazards warn; invalid input and unserved paths error.
    """
    root = Path(directory)
    findings: list[Finding] = []

    def add(severity, code, file, line, message, fix):
        findings.append(Finding(severity, code, file, line, message, fix))

    try:
        manifest = load_manifest(root)
    except UnicodeError:
        manifest = None
    if manifest is None:
        add(
            "error",
            "manifest-invalid",
            MANIFEST_FILENAME,
            1,
            "Missing or invalid manifest.",
            "Provide a valid UTF-8 protoagent.plugin.yaml with id and name.",
        )
        return findings

    def emitted(topic):
        return topic if topic == manifest.id or topic.startswith(manifest.id + ".") else f"{manifest.id}.{topic}"

    used = {"emits": set(), "subscribes": set()}
    routes = set()
    mounts: dict[str, set[str]] = {}
    tools = []
    files = []
    # Prune before descending: installed dependencies can contain thousands of
    # files, including paths too long to open on Windows.
    for directory, directories, filenames in os.walk(root):
        directories[:] = [name for name in directories if name not in _SKIP]
        files.extend(path for name in filenames if (path := Path(directory) / name).is_file())
    files.sort()
    for path in files:
        relative = path.relative_to(root).as_posix()
        if path.suffix not in {".py", ".js", ".html"}:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            add("error", "source-unreadable", relative, 1, str(exc), "Save readable UTF-8 source.")
            continue
        if path.suffix != ".py":
            if path.suffix == ".js" and "views" not in path.relative_to(root).parts:
                continue
            for match in _PAGE_OBJECT.finditer(source):
                topic_match = _TOPIC.search(match.group())
                if topic_match:
                    topic = emitted(topic_match[2])
                    used["emits"].add(topic)
                    if topic not in manifest.emits:
                        add(
                            "warn",
                            "page-publish-undeclared",
                            relative,
                            source.count("\n", 0, match.start()) + 1,
                            f"Page publishes undeclared topic {topic!r}.",
                            f"Add {topic!r} to manifest emits.",
                        )
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            add("error", "python-syntax", relative, exc.lineno or 1, exc.msg, "Fix the Python syntax before shipping.")
            continue
        nodes = list(ast.walk(tree))
        future = next(
            (
                n
                for n in nodes
                if isinstance(n, ast.ImportFrom)
                and n.module == "__future__"
                and any(a.name == "annotations" for a in n.names)
            ),
            None,
        )
        prefixes = {}
        registered_tools = set()
        for node in nodes:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and _name(node.value.func).endswith("APIRouter")
            ):
                for target in node.targets:
                    prefixes[_name(target)] = _string(_argument(node.value, "prefix", 999)) or ""
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr
            receiver = _name(node.func.value)
            if receiver == "registry" or receiver.endswith(".registry"):
                if method in {"emit", "on"}:
                    topic = _string(_argument(node, "topic"))
                    if topic is not None:
                        kind = "emits" if method == "emit" else "subscribes"
                        topic = emitted(topic) if kind == "emits" else topic
                        used[kind].add(topic)
                        if topic not in getattr(manifest, kind):
                            add(
                                "warn",
                                f"{kind}-undeclared",
                                relative,
                                node.lineno,
                                f"Undeclared {kind} topic {topic!r}.",
                                f"Add {topic!r} to manifest {kind}.",
                            )
                if method == "register_router":
                    router = _name(_argument(node, "router"))
                    prefix_node = _argument(node, "prefix", 1)
                    prefix = _string(prefix_node)
                    if prefix_node is None or isinstance(prefix_node, ast.Constant) and prefix_node.value is None:
                        prefix = f"/plugins/{manifest.id}"
                    if prefix is not None:
                        mounts.setdefault(router, set()).add(prefix)
                if method in {"register_tool", "register_tools"}:
                    tools.append((relative, node.lineno))
                    arg = _argument(node, "tool" if method == "register_tool" else "tools")
                    registered_tools.update(
                        _name(n) for n in (arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg])
                    )
        has_routes = False
        for node in nodes:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [d.func if isinstance(d, ast.Call) else d for d in node.decorator_list]
            is_tool = node.name in registered_tools or any(_name(d).split(".")[-1] == "tool" for d in decorators)
            route_decorators = [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in _ROUTE_METHODS
            ]
            has_routes |= bool(route_decorators)
            for dec in route_decorators:
                route = _string(_argument(dec, "path"))
                if route is not None:
                    routes.add((_name(dec.func.value), prefixes.get(_name(dec.func.value), "") + route))
            for param in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                annotation = _string(param.annotation) or _name(param.annotation)
                if (
                    is_tool
                    and param.arg in _TOOL_RESERVED_PARAMS
                    and not _tool_param_annotation_is_injected(param.arg, annotation)
                ):
                    add(
                        "warn",
                        "tool-reserved-param",
                        relative,
                        param.lineno,
                        f"Tool {node.name!r} uses reserved parameter {param.arg!r}.",
                        "Rename the parameter, or annotate it with its intended LangChain injection type.",
                    )
                if (
                    route_decorators
                    and (future or _string(param.annotation))
                    and any(_annotation_names_type(annotation, t) for t in _ASGI_INJECTED_TYPES)
                ):
                    add(
                        "warn",
                        "route-string-annotation",
                        relative,
                        param.lineno,
                        f"Route parameter {param.arg!r} has a string ASGI injection annotation.",
                        "Import the ASGI type at module scope and use an unquoted annotation; remove future annotations.",
                    )
        if future and (
            has_routes
            or any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "register_router"
                for n in nodes
            )
        ):
            add(
                "warn",
                "route-future-annotations",
                relative,
                future.lineno,
                "Route module enables postponed annotations.",
                "Remove from __future__ import annotations from this route module.",
            )

    for kind in used:
        for topic in sorted(set(getattr(manifest, kind)) - used[kind]):
            add(
                "warn",
                f"{kind}-unused",
                MANIFEST_FILENAME,
                1,
                f"Declared {kind} topic {topic!r} has no literal use.",
                f"Use the topic in code or remove it from {kind}; review dynamic uses manually.",
            )
    # A route decorator's receiver (``router``) rarely unparses to the text of the
    # ``register_router`` argument that mounts it — the router is built in a factory
    # (``register_router(_build_view_router(), …)``, as the artifact plugin does),
    # aliased/qualified, or defined in another file — so a route cannot be tied to one
    # specific mount statically. Treat every literal mount prefix (plus a bare fallback)
    # as applicable to every route. Over-generating served routes can only MISS a real
    # error, never invent the false ``public-path-missing`` that tying a factory-built
    # router to an empty prefix produced (#4809).
    mount_prefixes = {prefix for prefixes in mounts.values() for prefix in prefixes} | {""}
    served_routes = {prefix + route for _, route in routes for prefix in mount_prefixes}
    for public in manifest.public_paths:
        suffix = public
        for prefix in (f"/plugins/{manifest.id}/", f"/api/plugins/{manifest.id}/"):
            if public.startswith(prefix):
                suffix = public[len(prefix) :]
                break
        candidate = (root / suffix.lstrip("/")).resolve()
        exists = candidate.is_relative_to(root.resolve()) and candidate.exists()
        if not exists and not any(route.startswith(public) for route in served_routes):
            add(
                "error",
                "public-path-missing",
                MANIFEST_FILENAME,
                1,
                f"Public path {public!r} has no file or literal route.",
                "Add the file or route, correct public_paths, or make dynamic routing statically visible.",
            )
    if tools and not (root / "skills").is_dir():
        file, line = tools[0]
        add(
            "warn",
            "tools-without-skills",
            file,
            line,
            "Plugin registers tools but ships no skills/ directory.",
            "Add skills/<name>/SKILL.md describing how to use the tools.",
        )
    return sorted(findings, key=lambda f: (f.file, f.line, f.code, f.message))
