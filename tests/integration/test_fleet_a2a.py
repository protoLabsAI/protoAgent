"""Cross-instance A2A federation — the real wire path.

Boots a real instance and drives the actual ``A2aAdapter.dispatch`` (the code the
``delegate_to`` tool runs) against its ``/a2a`` endpoint: SendMessage → GetTask
poll → text extraction, with the new configurable poll timeout. This is the
federation path the same-box-vs-cross-machine fleet relies on; a loopback peer
stands in for a remote one (CI has no LAN/tailnet).
"""

from __future__ import annotations

import asyncio

from plugins.delegates.adapters import A2aAdapter, Delegate
from tests.integration.conftest import requires_integration

pytestmark = requires_integration


def test_adapter_dispatches_to_real_instance(fleet, monkeypatch):
    peer = fleet(name="peer")
    # We're exercising the A2A wire path, not the egress policy — allow the loopback url.
    monkeypatch.setattr("security.policy.check_url", lambda *_a, **_k: None)

    d = Delegate(name="peer", type="a2a", url=f"{peer.base}/a2a", poll_timeout_s=120)
    out = asyncio.run(A2aAdapter().dispatch(d, "ping"))

    assert isinstance(out, str) and out.strip(), f"adapter returned no text: {out!r}"
