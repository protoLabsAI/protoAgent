"""A plugin's deep engine logic — the kind of module that, today, can only be tested
if it's hand-extracted to be dependency-free. Here it's a sibling module the harness
can reach via the loaded package."""

from __future__ import annotations


def classify(items: list[dict]) -> dict:
    """Split items into 'big' (size >= 10) and 'small' — stand-in for real engine logic."""
    return {
        "big": [i["name"] for i in items if i.get("size", 0) >= 10],
        "small": [i["name"] for i in items if i.get("size", 0) < 10],
    }
