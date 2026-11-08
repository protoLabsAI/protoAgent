"""On-demand PV4 extraction. Run with python -m evals.memory_fixtures.build_pv4."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import urlopen

from evals.memory_fixtures import FIELDS
from knowledge.memories import MemoryStore

SEED = 125
DEFAULT_USERS = tuple(sorted(random.Random(SEED).sample(range(126, 156), 12)))
DEFAULT_OUTPUT = Path(__file__).parent / "pv4"


def fetch_page(offset: int, length: int) -> dict:
    query = urlencode(dict(dataset="memorilla/PersonalizationV4", config="default",
                           split="test", offset=offset, length=length))
    with urlopen("https://datasets-server.huggingface.co/rows?" + query, timeout=60) as response:
        return json.load(response)


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


def transcript(document):
    if isinstance(document, str) and document.strip():
        return document
    if isinstance(document, list) and document:
        return "\n".join(f"{turn['role']}: {turn['content']}" for turn in document)
    raise ValueError("expected a nonempty chat string or list of role/content messages")


async def build(output=DEFAULT_OUTPUT, *, users=DEFAULT_USERS, count=40, seed=SEED,
                config=None, yes=False, force=False, fetch=fetch_page, extract=None):
    """Build frozen fixtures; fetch and extraction seams support offline tests."""
    if not users or len(set(users)) != len(users):
        raise ValueError("users must be nonempty and unique")
    rows = fetch_users(users, fetch)
    cases = select_cases(rows, count, seed)
    output = Path(output)
    pending = [uid for uid in sorted(rows) if force or not (output / f"user_{uid}.jsonl").exists()]
    chats = {}
    for uid in pending:
        documents = rows[uid][0]["documents"]
        if len(documents) != 200:
            raise ValueError(f"user {uid}: expected 200 chats, got {len(documents)}")
        if any(row["documents"] != documents for row in rows[uid]):
            raise ValueError(f"user {uid}: inconsistent documents across questions")
        chats[uid] = [transcript(doc) for doc in documents]
    print(f"Extraction calls: {sum(map(len, chats.values()))}; users: {len(pending)}; "
          f"skipped: {len(rows) - len(pending)}")
    if chats and not yes and input("Continue? [y/N] ").strip().lower() not in {"y", "yes"}:
        return
    if extract is None:
        from graph.memory_facts import extract_and_store_facts
        extract = extract_and_store_facts
    output.mkdir(parents=True, exist_ok=True)
    for uid, documents in chats.items():
        with TemporaryDirectory(prefix=f"pv4-u{uid}-") as directory:
            store = MemoryStore(Path(directory) / "agent.db")
            for index, chat in enumerate(documents):
                await extract(chat, memory_store=store, config=config,
                              source=f"pv4-u{uid}-c{index}",
                              as_of=(date(2000, 1, 1) + timedelta(days=index)).isoformat())
            with closing(store._connect()) as db:
                exported = [dict(row) for row in db.execute(
                    f"SELECT {', '.join(FIELDS)} FROM memories ORDER BY id"
                )]
            write_jsonl(output / f"user_{uid}.jsonl", exported)
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
    from graph.config import LangGraphConfig
    from infra.paths import instance_paths

    config = LangGraphConfig.from_yaml(instance_paths().config_yaml)
    if args.model:
        config.aux_model = args.model
    asyncio.run(build(args.output, users=args.users, count=args.count, seed=args.seed,
                      config=config, yes=args.yes, force=args.force))


if __name__ == "__main__":
    main()
