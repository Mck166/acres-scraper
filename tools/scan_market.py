"""One-off scan of every active Nova Scotia listing.

    python -m tools.scan_market                 # enumerate only
    python -m tools.scan_market --yes --limit 25
    python -m tools.scan_market --yes
    python -m tools.scan_market --yes --resume

Uses the enumerator chosen by ``tools.probe_inventory``. Fetches each listing
and writes it to Mongo without flooding ``recent_updates``: the home deck only
gets a row when listed_on / price_changed_on / pending_on / sold_on is Atlantic
today. Takes the scrape lock so cron cannot overlap.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
from typing import Dict, Optional, Set

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper.backfill import LOCK_REFRESH_EVERY  # noqa: E402
from scraper.config import get_settings  # noqa: E402
from scraper.geocode import CachedGeocoder  # noqa: E402
from scraper.identity import resolve_listing_id  # noqa: E402
from scraper.inventory import (  # noqa: E402
    cutsheet_url_for,
    enumerate_market,
    has_atlantic_today_event,
    load_probe_result,
    probe,
)
from scraper.normalize import normalize_document, utcnow  # noqa: E402
from scraper.run import LOCK_NAME, configure_logging, open_client, refresh_api_index  # noqa: E402
from scraper.store import RunSummary, Store, connect  # noqa: E402
from scraper.sync import enrich_coordinates, merge_activity_dates  # noqa: E402

log = logging.getLogger("scan_market")


def listing_ids_with_listed_on(store: Store) -> Set[str]:
    ids: Set[str] = set()
    for collection in (store.properties, store.sold):
        cursor = collection.find(
            {"listed_on": {"$exists": True, "$ne": None}},
            {"listing_id": 1, "url": 1},
        )
        for doc in cursor:
            listing_id = resolve_listing_id(doc)
            if listing_id:
                ids.add(str(listing_id))
    return ids


def scan_market(
    store: Store,
    apply_changes: bool = False,
    resume: bool = False,
    limit: Optional[int] = None,
    use_lock: bool = True,
    capture_map: bool = False,
) -> Dict[str, object]:
    settings = store.settings
    summary = RunSummary()
    store.ensure_indexes()

    if use_lock and not store.acquire_lock(LOCK_NAME, settings.run_lock_ttl_seconds):
        raise RuntimeError("another scraper run is already in progress")

    skipped_resume = 0
    recorded = 0
    geocoder = CachedGeocoder(store.geocode_cache)

    try:
        with open_client(settings) as client:
            probe_result = load_probe_result()
            if probe_result is None:
                log.info("No probe result on disk; probing now")
                probe_result = probe(client, capture_map=capture_map)

            entries = enumerate_market(client, probe_result)
            if limit is not None:
                entries = entries[:limit]

            known = listing_ids_with_listed_on(store) if resume else set()
            todo = []
            for entry in entries:
                listing_id = str(entry.get("listing_id") or "")
                if resume and listing_id in known:
                    skipped_resume += 1
                    continue
                todo.append(entry)

            log.info(
                "Market scan: %d enumerated, %d to fetch, %d skipped (resume), dry_run=%s",
                len(entries),
                len(todo),
                skipped_resume,
                not apply_changes,
            )

            if not apply_changes:
                return {
                    "enumerated": len(entries),
                    "to_fetch": len(todo),
                    "skipped_resume": skipped_resume,
                    "dry_run": True,
                    "chosen": (probe_result or {}).get("chosen"),
                }

            for index, entry in enumerate(todo, start=1):
                if index % LOCK_REFRESH_EVERY == 0:
                    store.refresh_lock(LOCK_NAME, settings.run_lock_ttl_seconds)

                url = cutsheet_url_for(entry, settings.viewpoint_base_url)
                listing_id = str(entry.get("listing_id") or "")
                try:
                    raw = client.fetch_listing(url)
                    if not raw:
                        summary.errors.append(f"empty listing at {url}")
                        continue
                    merge_activity_dates(raw, entry)
                    document = normalize_document(raw, url=url)
                    document = enrich_coordinates(document, geocoder)
                    result = store.save_listing(document)
                    summary.detail_fetches += 1
                    summary.record(result)
                    if has_atlantic_today_event(document):
                        store.record_change(result, document)
                        recorded += 1
                    log.info("%s %s", listing_id, result.action)
                except Exception as exc:
                    log.error("Failed %s: %s", url, exc)
                    summary.errors.append(f"{url}: {exc}")

        summary.finished_at = utcnow()
        if apply_changes and summary.writes:
            refresh_api_index(settings)

        return {
            "enumerated": len(entries),
            "fetched": summary.detail_fetches,
            "skipped_resume": skipped_resume,
            "recorded_feed": recorded,
            "new": summary.new_listings,
            "price": summary.price_changes,
            "sold": summary.sold,
            "unchanged": summary.unchanged,
            "errors": len(summary.errors),
            "dry_run": False,
            "chosen": (probe_result or {}).get("chosen"),
        }
    finally:
        if use_lock:
            store.release_lock(LOCK_NAME)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="fetch listings and write to Mongo")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip listing ids that already have a listed_on date",
    )
    parser.add_argument("--limit", type=int, default=None, help="fetch at most N listings")
    parser.add_argument("--no-lock", action="store_true", help="do not take the scrape lock")
    parser.add_argument(
        "--capture-map",
        action="store_true",
        help="if probing is needed, also capture the /map SPA network log",
    )
    args = parser.parse_args(argv)

    configure_logging()
    settings = get_settings()
    if not settings.viewpoint_user or not settings.viewpoint_pass:
        log.error("VIEWPOINT_USER and VIEWPOINT_PASS must be set")
        return 1

    store = Store(connect(settings), settings)
    log.info(
        "Database: %s dry_run=%s resume=%s limit=%s",
        store.db.name,
        not args.yes,
        args.resume,
        args.limit,
    )

    try:
        if not args.yes:
            log.info("Dry run. Nothing will be written. Re-run with --yes to apply.")
        results = scan_market(
            store,
            apply_changes=args.yes,
            resume=args.resume,
            limit=args.limit,
            use_lock=not args.no_lock,
            capture_map=args.capture_map,
        )
        log.info("Done: %s", results)
        return 0
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    except Exception:
        log.exception("Market scan failed")
        return 1
    finally:
        store.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
