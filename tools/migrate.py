"""One-off migration of an existing database onto the new shape.

Run once, in order:

    python -m tools.migrate            # report what would change, write nothing
    python -m tools.migrate --yes      # do it

The steps are:

1. back every collection up to a timestamped folder of BSON-safe JSON
2. refuse to continue if two active listings share a listing id, because the
   scraper is about to put a unique index on it
3. re-normalize stored documents, which is what gives listings currently
   labelled `NEW PRICE` a status the map is willing to draw
4. move sold listings into the archive, keeping their `_id` so favourites
   still resolve
5. create the indexes
6. rebuild photo sets that never extracted properly

Every step is safe to run twice.
"""

import argparse
import logging
import pathlib
import sys
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional

from bson import json_util

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper.backfill import (  # noqa: E402
    archive_existing_sold,
    backfill_listing_ids,
    repair_photos,
)
from scraper.config import get_settings  # noqa: E402
from scraper.identity import resolve_listing_id  # noqa: E402
from scraper.normalize import normalize_document  # noqa: E402
from scraper.run import configure_logging, open_client  # noqa: E402
from scraper.store import Store, connect  # noqa: E402

log = logging.getLogger("migrate")

BACKUP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "backups"


def backup(store: Store, root: pathlib.Path = BACKUP_ROOT) -> pathlib.Path:
    """Write every collection to disk before anything is touched.

    mongodump is not installed on this machine, so the dump is written as
    extended JSON, which round-trips BSON types (ObjectId, datetime) exactly.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = root / f"{store.db.name}_{stamp}"
    destination.mkdir(parents=True, exist_ok=True)

    for name in store.db.list_collection_names():
        documents = list(store.db[name].find({}))
        (destination / f"{name}.json").write_text(json_util.dumps(documents, indent=2))
        log.info("Backed up %s: %d documents", name, len(documents))

    return destination


def restore(store: Store, source: pathlib.Path) -> Dict[str, int]:
    """Put a backup back, document for document."""
    restored: Dict[str, int] = {}
    for path in sorted(source.glob("*.json")):
        documents = json_util.loads(path.read_text())
        collection = store.db[path.stem]
        for document in documents:
            collection.replace_one({"_id": document["_id"]}, document, upsert=True)
        restored[path.stem] = len(documents)
        log.info("Restored %s: %d documents", path.stem, len(documents))
    return restored


def duplicate_listing_ids(collection) -> Dict[str, int]:
    """Listing ids held by more than one document."""
    counts: Counter = Counter()
    for doc in collection.find({}, {"listing_id": 1, "url": 1}):
        listing_id = resolve_listing_id(doc)
        if listing_id:
            counts[listing_id] += 1
    return {listing_id: count for listing_id, count in counts.items() if count > 1}


def renormalize(collection, dry_run: bool = True) -> Dict[str, int]:
    """Bring stored documents up to the current normalized shape."""
    summary = {"examined": 0, "changed": 0}

    for doc in collection.find({}):
        summary["examined"] += 1
        normalized = normalize_document(dict(doc), url=doc.get("url"))

        changes = {
            key: value
            for key, value in normalized.items()
            if key != "_id" and doc.get(key) != value
        }
        if not changes:
            continue

        summary["changed"] += 1
        if dry_run:
            log.info("%s would change: %s", doc.get("url"), sorted(changes))
            continue

        collection.update_one({"_id": doc["_id"]}, {"$set": changes})

    return summary


def status_breakdown(collection) -> Dict[str, int]:
    counts: Counter = Counter()
    for doc in collection.find({}, {"Status": 1}):
        counts[str(doc.get("Status") or "")] += 1
    return dict(counts)


def migrate(
    store: Store,
    apply_changes: bool = False,
    photo_limit: Optional[int] = None,
    skip_photos: bool = False,
) -> Dict[str, object]:
    dry_run = not apply_changes
    results: Dict[str, object] = {}

    log.info("Before: %s", status_breakdown(store.properties))

    duplicates = duplicate_listing_ids(store.properties)
    results["duplicate_listing_ids"] = duplicates
    if duplicates:
        # A unique index cannot be created over these, and picking a winner is a
        # judgement call rather than a migration step.
        log.error("Duplicate listing ids must be resolved by hand first: %s", duplicates)
        return results

    results["normalized"] = renormalize(store.properties, dry_run=dry_run)
    log.info("Normalized: %s", results["normalized"])

    results["listing_ids"] = backfill_listing_ids(store.properties, dry_run=dry_run)
    log.info("Listing ids: %s", results["listing_ids"])

    results["archived"] = archive_existing_sold(store, dry_run=dry_run)
    log.info("Archived: %s", results["archived"])

    if apply_changes:
        store.ensure_indexes()
        log.info("Indexes created")

    if not skip_photos:
        with open_client() as client:
            repairs: List[Dict[str, int]] = []
            for collection in (store.properties, store.sold):
                repairs.append(repair_photos(client, collection, limit=photo_limit, dry_run=dry_run))
        results["photos"] = repairs
        log.info("Photos: %s", repairs)

    log.info("After: %s", status_breakdown(store.properties))
    log.info(
        "Collections: %d active, %d archived",
        store.properties.count_documents({}),
        store.sold.count_documents({}),
    )
    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually write the changes")
    parser.add_argument("--skip-photos", action="store_true", help="leave photo sets alone")
    parser.add_argument("--photo-limit", type=int, default=None, help="repair at most N photo sets")
    parser.add_argument("--no-backup", action="store_true", help="skip the backup (not advised)")
    parser.add_argument("--restore", metavar="DIR", help="restore a backup instead of migrating")
    args = parser.parse_args(argv)

    configure_logging()
    settings = get_settings()
    store = Store(connect(settings), settings)

    log.info("Database: %s", store.db.name)

    try:
        if args.restore:
            restore(store, pathlib.Path(args.restore))
            return 0

        if not args.no_backup:
            log.info("Backup written to %s", backup(store))

        if not args.yes:
            log.info("Dry run. Nothing was written. Re-run with --yes to apply.")

        migrate(
            store,
            apply_changes=args.yes,
            photo_limit=args.photo_limit,
            skip_photos=args.skip_photos,
        )
        return 0
    finally:
        store.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
