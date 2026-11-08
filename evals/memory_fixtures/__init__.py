"""Frozen memory fixtures, independent of the live evaluation gates."""

import json
import weakref
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from knowledge.memories import MemoryStore

FIELDS = (
    "id", "kind", "subject", "content", "delivery", "review_state", "provenance",
    "source_session", "superseded_by", "valid_from",
)


def load_fixture_store(path, embed_fn) -> MemoryStore:
    """Rebuild an isolated store; its temporary directory lives as long as the store.

    IDs and audit links are restored verbatim without running the write funnel.
    Embeddings are computed locally, never read from the fixture.
    """
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = {row["id"] for row in rows}
    if len(ids) != len(rows) or any(
        row["superseded_by"] is not None and row["superseded_by"] not in ids for row in rows
    ):
        raise ValueError("duplicate IDs or dangling supersession links in fixture")
    directory = TemporaryDirectory(prefix="pv4-load-")
    try:
        store = MemoryStore(Path(directory.name) / "agent.db", embed_fn=embed_fn)
        # All rows first, then links: also supports forward references and sparse IDs.
        with closing(store._connect()) as db, db:
            for row in rows:
                values = {field: row[field] for field in FIELDS}
                values["superseded_by"] = None
                stamp = row["valid_from"] or "2000-01-01"
                db.execute(
                    f"INSERT INTO memories ({', '.join(FIELDS)}, created_at, updated_at) "
                    f"VALUES ({', '.join('?' for _ in range(len(FIELDS) + 2))})",
                    [*values.values(), stamp, stamp],
                )
                vec = store._embed(row["content"])
                if vec is not None:
                    db.execute("INSERT INTO memory_vectors VALUES (?, ?)", (row["id"], json.dumps(vec)))
            for row in rows:
                db.execute("UPDATE memories SET superseded_by = ? WHERE id = ?",
                           (row["superseded_by"], row["id"]))
    except BaseException:
        directory.cleanup()
        raise
    weakref.finalize(store, directory.cleanup)
    return store
