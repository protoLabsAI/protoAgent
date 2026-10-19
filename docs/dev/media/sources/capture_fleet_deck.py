import asyncio
from pathlib import Path
from deck.app import FleetDeck
from deck.data import LiveBackend
from deck.hub import connect


async def main():
    conn = connect(url="http://127.0.0.1:7896")
    app = FleetDeck(LiveBackend(conn), poll_s=1)
    async with app.run_test(size=(132, 16)) as pilot:
        await pilot.pause(5)
        Path("/tmp/docs-fleet-deck-overview.svg").write_text(app.export_screenshot(), encoding="utf-8")
        print("Captured actual live roster", [(r["name"], r.get("running")) for r in app.snapshot.roster])


asyncio.run(main())
