"""Fleet supervisor (ADR 0042) — run a fleet of workspace agents as persistent
background processes, with start / stop / status from a control plane.

This is slice 1: process lifecycle over ADR 0041 workspaces (``run_exec`` is the
launch primitive). The hub/proxy + console switcher (slices 2-3) build on top. No new
runtime — each agent is a normal ``python -m server --ui none`` on its workspace's port.
"""
