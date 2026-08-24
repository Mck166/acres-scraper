"""One-off backfill of listing descriptions into MongoDB.

Run against the same database the scraper uses:

    python -m tools.backfill_descriptions              # report what would change
    python -m tools.backfill_descriptions --yes        # write Description on every listing
    python -m tools.backfill_descriptions --yes --resume
    python -m tools.backfill_descriptions --yes --limit 25 --active-only

Does not rewrite photos, prices, status, or date_updated. Takes the scrape lock
so a scheduled run cannot overlap, and refreshes that lock as it goes.
"""

import argparse
import logging
import pathlib
import sys
from typing import Dict, List, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper.backfill import LOCK_REFRESH_EVERY, backfill_descriptions  # noqa: E402
from scraper.config import get_settings  # noqa: E402
from scraper.run import LOCK_NAME, configure_logging, open_client  # noqa: E402
from scraper.store import Store, connect  # noqa: E402

log = logging.getLogger("backfill_descriptions")


def _merge(left: Dict[str, int], right: Dict[str, int]) -> Dict[str, int]:
    merged = dict(left)
    for key, value in right.items():
        merged[key] = merged.get(key, 0) + value
    return merged


def run(
    store: Store,
    apply_changes: bool = False,
    resume: bool = False,
    limit: Optional[int] = None,
    active_only: bool = False,
    use_lock: bool = True,
) -> Dict[str, object]:
    settings = store.settings
    dry_run = not apply_changes
    results: Dict[str, object] = {}

    collections: List[tuple] = [("active", store.properties)]
    if not active_only:
        collections.append(("sold", store.sold))

    if use_lock and not store.acquire_lock(LOCK_NAME, settings.run_lock_ttl_seconds):
        raise RuntimeError("another scraper run is already in progress")

    remaining = limit
    totals: Dict[str, int] = {}

    try:
        with open_client(settings) as client:

            def on_progress(index: int, _doc: dict) -> None:
                if index % LOCK_REFRESH_EVERY == 0:
                    store.refresh_lock(LOCK_NAME, settings.run_lock_ttl_seconds)

            for name, collection in collections:
                if remaining is not None and remaining <= 0:
                    results[name] = {
                        "examined": 0,
                        "updated": 0,
                        "unchanged": 0,
                        "skipped": 0,
                        "failed": 0,
                    }
                    continue

                summary = backfill_descriptions(
                    client,
                    collection,
                    limit=remaining,
                    dry_run=dry_run,
                    resume=resume,
                    on_progress=on_progress if use_lock else None,
                )
                results[name] = summary
                totals = _merge(totals, summary)
                log.info("%s: %s", name, summary)
                if remaining is not None:
                    remaining -= summary["examined"]
    finally:
        if use_lock:
            store.release_lock(LOCK_NAME)

    results["total"] = totals
    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually write the changes")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip listings that already have a Description",
    )
    parser.add_argument("--limit", type=int, default=None, help="visit at most N listings")
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="leave the sold archive alone",
    )
    parser.add_argument("--no-lock", action="store_true", help="do not take the scrape lock")
    args = parser.parse_args(argv)

    configure_logging()
    settings = get_settings()
    store = Store(connect(settings), settings)

    log.info(
        "Database: %s dry_run=%s resume=%s limit=%s active_only=%s",
        store.db.name,
        not args.yes,
        args.resume,
        args.limit,
        args.active_only,
    )

    try:
        if not args.yes:
            log.info("Dry run. Nothing will be written. Re-run with --yes to apply.")

        results = run(
            store,
            apply_changes=args.yes,
            resume=args.resume,
            limit=args.limit,
            active_only=args.active_only,
            use_lock=not args.no_lock,
        )
        log.info("Done: %s", results.get("total"))
        return 0
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    except Exception:
        log.exception("Description backfill failed")
        return 1
    finally:
        store.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
