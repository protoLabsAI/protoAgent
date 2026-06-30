"""Workspaces (ADR 0041) — a named, self-contained agent on the host.

A workspace bundles the isolation knobs into one object: an instance root
(``PROTOAGENT_HOME=<ws>`` → config at ``<ws>/config``, plugins at ``<ws>/plugins``),
an instance id (``PROTOAGENT_INSTANCE`` → scoped data stores), and a port. The
``workspace`` CLI manages them; this package is the thin orchestration layer over
the existing primitives — it adds no new runtime.
"""
