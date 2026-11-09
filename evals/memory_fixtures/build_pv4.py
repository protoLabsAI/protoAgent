"""On-demand PV4 extraction. Run with python -m evals.memory_fixtures.build_pv4."""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import importlib.util
import json
import os
import random
import time
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from re import sub
from sqlite3 import Error as SQLiteError
from sqlite3 import connect as sqlite_connect
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from evals.memory_fixtures import FIELDS
from knowledge.memories import MemoryStore

SEED = 125
DEFAULT_USERS = tuple(sorted(random.Random(SEED).sample(range(126, 156), 12)))
DEFAULT_OUTPUT = Path(__file__).parent / "pv4"
# Per-chat checkpoints live OUTSIDE the checkout: a board coder round deletes and
# recreates its worktree, so progress kept under the repo is lost every round and the
# next round pays for every extraction call again (bd-9w23j, ~2,400 calls per attempt).
WORK_ROOT_ENV = "PV4_WORK_DIR"
DEFAULT_WORK_ROOT = Path.home() / ".cache" / "protoagent" / "pv4-extract"


def fetch_page(offset: int, length: int) -> dict:
    query = urlencode(dict(dataset="memorilla/PersonalizationV4", config="default",
                           split="test", offset=offset, length=length))
    for attempt in range(3):
        try:
            with urlopen("https://datasets-server.huggingface.co/rows?" + query, timeout=60) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in {429, 502, 503, 504} or attempt == 2:
                raise
            print(f"PV4 offset {offset}: HTTP {exc.code}; retrying", flush=True)
            exc.close()
            time.sleep(10 * (attempt + 1))


def fetch_users(users, fetch=fetch_page):
    """Page the test split and retain only the requested users' question rows."""
    selected = defaultdict(list)
    offset = 0
    while True:
        page = fetch(offset, 100)
        rows = page["rows"]
        if not rows:
            if offset < page["num_rows_total"]:
                raise ValueError("PV4 API returned an incomplete page")
            break
        for item in rows:
            row = item["row"]
            uid = int(row["user_id"])
            if uid in users:
                if item.get("truncated_cells"):
                    raise ValueError(f"user {uid}: API returned truncated cells")
                if selected[uid]:
                    documents = selected[uid][0]["documents"]
                    if row["documents"] != documents:
                        raise ValueError(f"user {uid}: inconsistent documents across questions")
                    # The API repeats 200 chats per question; retain one copy per user.
                    row = {**row, "documents": documents}
                selected[uid].append(row)
        offset += len(rows)
        if offset >= page["num_rows_total"]:
            break
    missing = set(users) - selected.keys()
    if missing:
        raise ValueError(f"PV4 test split missing users: {sorted(missing)}")
    return selected


def select_cases(rows, count=40, seed=SEED):
    """Round-robin users, sampling hard questions first within each user."""
    if count < 1 or count > sum(len(group) for group in rows.values()):
        raise ValueError("case count must be positive and no larger than available questions")
    rng = random.Random(seed)
    queues = {}
    for uid in sorted(rows):
        queue = list(enumerate(rows[uid]))
        rng.shuffle(queue)
        queue.sort(key=lambda pair: not pair[1]["hard"])
        queues[uid] = queue
    result = []
    while len(result) < count:
        for uid, queue in queues.items():
            if queue:
                index, row = queue.pop(0)
                result.append(dict(id=f"pv4-u{uid}-q{index}", user_id=uid,
                                   **{key: row[key] for key in ("question", "answer", "choices", "hard")}))
                if len(result) == count:
                    break
    return result


def write_jsonl(path, rows):
    """Publish complete files only; an interrupted user is safe to retry."""
    temporary = path.with_suffix(".jsonl.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def default_work_dir(config) -> Path:
    """Checkpoint directory for one extraction model, so a model change never resumes
    another model's partial store."""
    from graph.agent import _resolve_aux_model

    model = _resolve_aux_model(config, "") or getattr(config, "model_name", "") or "default"
    root = Path(os.environ.get(WORK_ROOT_ENV) or DEFAULT_WORK_ROOT)
    return root / (sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_") or "default")


# The code that turns a chat into stored rows. A checkpoint taken under different
# extraction or consolidation code must not be resumed: its rows came from other logic.
EXTRACTION_MODULES = ("graph.memory_facts", "knowledge.memories")
EXTRACTION_ATTEMPTS = 3


def extraction_code_key() -> str:
    digest = hashlib.sha256()
    for name in EXTRACTION_MODULES:
        spec = importlib.util.find_spec(name)
        digest.update(name.encode())
        if spec is not None and spec.origin:
            digest.update(Path(spec.origin).read_bytes())
    return digest.hexdigest()


def chats_digest(chats, code_key: str = "") -> str:
    payload = json.dumps([code_key, chats], ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ExtractionFailed(RuntimeError):
    """A chat's extraction kept failing; the checkpoint stops before it so a rerun retries it."""


def checkpoint_done(checkpoint: Path, digest: str) -> int:
    """Chats already extracted into ``checkpoint`` for exactly these chats; 0 when the
    checkpoint is missing, unreadable or was taken over different chats."""
    if not checkpoint.is_file():
        return 0
    try:
        with closing(sqlite_connect(checkpoint)) as db:
            row = db.execute("SELECT done, digest FROM pv4_progress").fetchone()
    except SQLiteError:
        return 0
    return int(row[0]) if row and row[1] == digest else 0


def save_checkpoint(store_path: Path, checkpoint: Path, done: int, digest: str) -> None:
    """Snapshot the working store with its progress in ONE file, replaced atomically:
    a crash leaves either the previous checkpoint or this one, never a store whose
    progress count disagrees with its rows (which would extract a chat twice)."""
    temporary = checkpoint.with_name(checkpoint.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with closing(sqlite_connect(store_path)) as src, closing(sqlite_connect(temporary)) as dst:
            src.backup(dst)
            dst.execute("CREATE TABLE IF NOT EXISTS pv4_progress (done INTEGER NOT NULL, digest TEXT NOT NULL)")
            dst.execute("DELETE FROM pv4_progress")
            dst.execute("INSERT INTO pv4_progress (done, digest) VALUES (?, ?)", (done, digest))
            dst.commit()
        os.replace(temporary, checkpoint)
    finally:
        temporary.unlink(missing_ok=True)


def restore_checkpoint(checkpoint: Path, store_path: Path) -> None:
    with closing(sqlite_connect(checkpoint)) as src, closing(sqlite_connect(store_path)) as dst:
        src.backup(dst)


def transcript(document):
    if isinstance(document, str) and document.strip():
        return document
    if isinstance(document, list) and document:
        return "\n".join(f"{turn['role']}: {turn['content']}" for turn in document)
    raise ValueError("expected a nonempty chat string or list of role/content messages")


async def build(output=DEFAULT_OUTPUT, *, users=DEFAULT_USERS, count=40, seed=SEED,
                config=None, yes=False, force=False, fetch=fetch_page, extract=None, work_dir=None,
                extractor=None, retry_delay_s=10.0):
    """Build frozen fixtures; fetch and extraction seams support offline tests.

    ``work_dir`` holds a per-user checkpoint taken after every chat, so an interrupted
    run (a killed process, an ended session, a deleted worktree) resumes at the next
    chat instead of re-extracting the user. A real run (one given a ``config``) uses
    ``default_work_dir(config)`` when none is passed; offline callers get no checkpoints.

    The runtime extractor never raises, so a failed model call would otherwise look like
    a chat with no facts and be checkpointed as done. With the default ``extract`` the
    model call (``extractor``, default the runtime one) is watched: a failing chat is
    retried, and if it still fails the run stops BEFORE checkpointing it."""
    if not users or len(set(users)) != len(users):
        raise ValueError("users must be nonempty and unique")
    if work_dir is None and config is not None:
        work_dir = default_work_dir(config)
    rows = fetch_users(users, fetch)
    cases = select_cases(rows, count, seed)
    output = Path(output)
    pending = [uid for uid in sorted(rows) if force or not (output / f"user_{uid}.jsonl").exists()]
    code_key = extraction_code_key() if work_dir is not None else ""
    chats, digests, done = {}, {}, {}
    for uid in pending:
        documents = rows[uid][0]["documents"]
        if len(documents) != 200:
            raise ValueError(f"user {uid}: expected 200 chats, got {len(documents)}")
        if any(row["documents"] != documents for row in rows[uid]):
            raise ValueError(f"user {uid}: inconsistent documents across questions")
        chats[uid] = [transcript(doc) for doc in documents]
        digests[uid] = chats_digest(chats[uid], code_key)
        checkpoint = Path(work_dir) / f"user_{uid}.db" if work_dir is not None else None
        if checkpoint is not None and force:
            checkpoint.unlink(missing_ok=True)
        done[uid] = checkpoint_done(checkpoint, digests[uid]) if checkpoint is not None else 0
    remaining = sum(len(chats[uid]) - done[uid] for uid in chats)
    resumed = sum(done.values())
    print(f"Extraction calls: {remaining}; users: {len(pending)}; "
          f"skipped: {len(rows) - len(pending)}"
          + (f"; resumed from checkpoint: {resumed} chats" if resumed else "")
          + (f"; checkpoints: {work_dir}" if work_dir is not None else ""), flush=True)
    if remaining and not yes and input("Continue? [y/N] ").strip().lower() not in {"y", "yes"}:
        return
    failures = []
    if extract is None:
        from graph.memory_facts import _default_extractor, extract_and_store_facts

        model_call = extractor or _default_extractor

        async def watched(transcript_text, cfg):
            try:
                return await model_call(transcript_text, cfg)
            except Exception as exc:
                failures.append(exc)
                raise

        extract = functools.partial(extract_and_store_facts, extractor=watched)
    output.mkdir(parents=True, exist_ok=True)
    if work_dir is not None:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
    for uid, documents in chats.items():
        checkpoint = Path(work_dir) / f"user_{uid}.db" if work_dir is not None else None
        with TemporaryDirectory(prefix=f"pv4-u{uid}-") as directory:
            store_path = Path(directory) / "agent.db"
            if done[uid]:
                restore_checkpoint(checkpoint, store_path)
            store = MemoryStore(store_path)
            for index in range(done[uid], len(documents)):
                for attempt in range(EXTRACTION_ATTEMPTS):
                    failures.clear()
                    await extract(documents[index], memory_store=store, config=config,
                                  source=f"pv4-u{uid}-c{index}",
                                  as_of=(date(2000, 1, 1) + timedelta(days=index)).isoformat())
                    if not failures:
                        break
                    print(f"user {uid} chat {index}: extraction failed ({failures[-1]!r}); "
                          f"attempt {attempt + 1}/{EXTRACTION_ATTEMPTS}", flush=True)
                    if attempt + 1 < EXTRACTION_ATTEMPTS:
                        await asyncio.sleep(retry_delay_s * (attempt + 1))
                else:
                    raise ExtractionFailed(
                        f"user {uid} chat {index}: extraction failed {EXTRACTION_ATTEMPTS} times; "
                        f"checkpointed through chat {index - 1}, rerun to retry from here"
                    )
                if checkpoint is not None:
                    save_checkpoint(store_path, checkpoint, index + 1, digests[uid])
            with closing(store._connect()) as db:
                exported = [dict(row) for row in db.execute(
                    f"SELECT {', '.join(FIELDS)} FROM memories ORDER BY id"
                )]
            write_jsonl(output / f"user_{uid}.jsonl", exported)
            # Extraction never raises; a zero-row user is indistinguishable from a
            # broken gateway, so surface the count for the mandatory fixture review.
            flag = " (no durable facts — check the extraction model)" if not exported else ""
            print(f"user {uid}: {len(exported)} stored rows{flag}", flush=True)
    write_jsonl(output / "cases.jsonl", cases)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, nargs="+", default=DEFAULT_USERS)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", help="Override routing.aux_model for extraction")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    from graph.agent import _resolve_aux_model
    from graph.config import LangGraphConfig
    from infra.paths import instance_paths

    config = LangGraphConfig.from_yaml(instance_paths().config_yaml)
    if args.model:
        config.aux_model = args.model
    print(f"Extraction model: {_resolve_aux_model(config, '') or config.model_name}", flush=True)
    asyncio.run(build(args.output, users=args.users, count=args.count, seed=args.seed,
                      config=config, yes=args.yes, force=args.force))


if __name__ == "__main__":
    main()
