"""Serve only reviewed, bundled documentation media (never a custom docs root)."""

from __future__ import annotations

import json
from pathlib import Path


def media_file(name: str) -> Path | None:
    base = Path(__file__).resolve().parents[2] / "docs/public/media"
    try:
        allowed = json.loads(Path(__file__).with_name("media-assets.json").read_text(encoding="utf-8"))["files"]
        if name not in allowed:
            return None
        target = (base / name).resolve()
        if not target.is_relative_to(base.resolve()) or not target.is_file():
            return None
        return target
    except (OSError, ValueError, KeyError, TypeError):
        return None
