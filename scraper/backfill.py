"""One-off repairs over documents that are already stored.

These run against existing data rather than against the activity feed, so they
live apart from the sync engine. Each is safe to run repeatedly.
"""

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

from .identity import class_id_from_url, resolve_listing_id
from .normalize import is_sold_status, parse_price, utcnow
from .photos import (
    PlaceholderProbe,
    extract_photo_set,
    normalize_photo_list,
    photo_urls_for,
    verify_photo_set,
)

log = logging.getLogger(__name__)

# A listing with one photo is almost always a listing whose photos failed to
# extract, not a listing with a single photo.
SUSPECT_PHOTO_COUNT = 1


def find_broken_photo_sets(collection, limit: Optional[int] = None) -> List[dict]:
    """Documents whose photo set looks like it failed to extract."""
    query = {
        "$or": [
            {"Photos": {"$exists": False}},
            {"Photos": {"$size": 0}},
            {"Photos": {"$size": SUSPECT_PHOTO_COUNT}},
        ]
    }
    cursor = collection.find(query, {"url": 1, "listing_id": 1, "Photos": 1, "Photo_Count": 1})
    if limit:
        cursor = cursor.limit(limit)
    return list(cursor)


def photos_for_listing(client, url: str, probe=None) -> List[str]:
    """Rebuild a listing's photo set from its cutsheet page."""
    listing_id = resolve_listing_id({"url": url})
    if not listing_id:
        return []

    html = client.fetch_page(url)
    photo_set = extract_photo_set(html, listing_id, class_id_from_url(url))
    if photo_set is None:
        return []

    if probe is not None:
        photo_set = verify_photo_set(photo_set, probe)

    return normalize_photo_list(photo_urls_for(photo_set, client.base_url))


def repair_photos(
    client,
    collection,
    limit: Optional[int] = None,
    dry_run: bool = False,
    on_result: Optional[Callable[[dict, List[str]], None]] = None,
) -> Dict[str, int]:
    """Rebuild photo sets for documents that are missing them."""
    broken = find_broken_photo_sets(collection, limit)
    log.info("Found %d documents with a suspect photo set", len(broken))

    summary = {"examined": len(broken), "repaired": 0, "unchanged": 0, "failed": 0}
    probe = PlaceholderProbe(client.session, client.base_url)

    for doc in broken:
        url = doc.get("url")
        if not url:
            summary["failed"] += 1
            continue

        try:
            photos = photos_for_listing(client, url, probe=probe)
        except Exception as exc:
            log.error("Could not rebuild photos for %s: %s", url, exc)
            summary["failed"] += 1
            continue

        if on_result is not None:
            on_result(doc, photos)

        before = len(doc.get("Photos") or [])
        if len(photos) <= before:
            summary["unchanged"] += 1
            continue

        if not dry_run:
            collection.update_one(
                {"_id": doc["_id"]},
                {"$set": {"Photos": photos, "Photo_Count": len(photos)}},
            )
        summary["repaired"] += 1
        log.info("%s: %d -> %d photos", url, before, len(photos))

    return summary


def backfill_listing_ids(collection, dry_run: bool = False) -> Dict[str, int]:
    """Give every stored document the listing id we now key on."""
    summary = {"examined": 0, "updated": 0, "skipped": 0}

    for doc in collection.find({"listing_id": {"$exists": False}}, {"url": 1}):
        summary["examined"] += 1
        listing_id = resolve_listing_id(doc)
        if not listing_id:
            summary["skipped"] += 1
            continue

        if not dry_run:
            collection.update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "listing_id": listing_id,
                        "listing_class_id": class_id_from_url(doc.get("url")),
                    }
                },
            )
        summary["updated"] += 1

    return summary


def archive_existing_sold(store, dry_run: bool = False) -> Dict[str, int]:
    """Move already-sold listings out of the active collection.

    Documents keep their ``_id`` so that saved favourites and dislikes, which
    reference properties by that id, keep resolving after the move.
    """
    summary = {"examined": 0, "archived": 0, "skipped": 0}
    now = utcnow()

    for doc in list(store.properties.find({})):
        if not is_sold_status(doc.get("Status")):
            continue

        summary["examined"] += 1
        archived = dict(doc)
        archived.setdefault("sold_at", doc.get("date_updated") or now)
        archived.setdefault("sold_price", parse_price(doc.get("Price")))
        archived["archived_at"] = now
        archived["archived_reason"] = "sold"

        date_added = doc.get("date_added")
        if date_added is not None and hasattr(archived["sold_at"], "__sub__"):
            try:
                archived["days_on_market"] = max((archived["sold_at"] - date_added).days, 0)
            except TypeError:
                pass

        if dry_run:
            summary["archived"] += 1
            continue

        store.sold.replace_one({"_id": doc["_id"]}, archived, upsert=True)
        store.properties.delete_one({"_id": doc["_id"]})
        summary["archived"] += 1

    return summary


def copy_documents(source, target, documents: Iterable[Dict[str, Any]]) -> int:
    """Copy documents between collections, used to seed test databases."""
    count = 0
    for doc in documents:
        target.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        count += 1
    return count
