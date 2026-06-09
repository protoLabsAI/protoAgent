"""Workspaces (ADR 0041) — a named, self-contained agent on the host.

A workspace bundles the three isolation knobs into one object: a config dir
(``PROTOAGENT_CONFIG_DIR``), an instance id (``instance.id`` → scoped data), and a
port. The ``workspace`` CLI manages them; this package is the thin orchestration
layer over the existing primitives — it adds no new runtime.
"""
