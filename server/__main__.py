"""``python -m server`` entry point.

ADR 0023 promoted the former top-level ``server.py`` into this package
(``server/__init__.py`` is the composition root). The container entrypoint, the
eval sweep, and the PyInstaller sidecar build all launch the server through this
module so the package resolves as ``server`` rather than a loose script.
"""

from server import _main

if __name__ == "__main__":
    _main()
