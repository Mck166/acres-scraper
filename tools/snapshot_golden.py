"""Capture a handful of real production documents as test fixtures.

The snapshot is what proves the rewrite still produces documents the app, the
website, and Acres-API can read. Run it again if the production shape changes.

    python tools/snapshot_golden.py
"""

import json
import os
import pathlib
import sys

from bson import json_util

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper.config import get_settings  # noqa: E402
from scraper.store import connect  # noqa: E402

OUTPUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "golden"


def pick_samples(collection):
    """One document per interesting shape, so the fixtures stay representative."""
    wanted = [
        ("for_sale", {"Status": "FOR SALE", "Photos.5": {"$exists": True}}),
        ("new_price", {"Status": "NEW PRICE"}),
        ("sold", {"Status": "SOLD"}),
        ("missing_photos", {"$or": [{"Photos": {"$size": 0}}, {"Photos": {"$size": 1}}]}),
        ("no_coordinates", {"latitude": None}),
    ]

    samples = {}
    for name, query in wanted:
        doc = collection.find_one(query)
        if doc is not None:
            samples[name] = doc
        else:
            print(f"  ! no document matched {name}: {query}")
    return samples


def main() -> int:
    settings = get_settings()
    client = connect(settings)
    try:
        collection = client[settings.mongodb_db_name][settings.properties_collection]
        samples = pick_samples(collection)

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        for name, doc in samples.items():
            path = OUTPUT_DIR / f"{name}.json"
            path.write_text(json.dumps(json.loads(json_util.dumps(doc)), indent=2, sort_keys=True))
            print(f"  wrote {path.name}: {len(doc)} fields, {len(doc.get('Photos') or [])} photos")

        print(f"Saved {len(samples)} golden documents to {OUTPUT_DIR}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
