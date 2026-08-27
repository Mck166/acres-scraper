"""Entry point for a scraper run.

A run takes a lock in MongoDB before it starts. Cron fires every six hours and a
run that hits a slow site can outlast its slot, so without the lock two runs
could archive and re-insert the same listing at the same time. The lock carries
a TTL, so a container killed mid-run frees it without anyone intervening.

Every run, successful or not, leaves a row in `scrape_runs`. That is the only
way a missed window becomes visible: the activity feed has no backfill, so a day
the scraper did not run is a day of listings that will never arrive.
"""

import logging
import sys
from typing import Any, List, Optional

import requests

from .config import Settings, get_settings
from .normalize import utcnow
from .selenium_client import SeleniumClient
from .store import RunSummary, Store, connect

log = logging.getLogger(__name__)

LOCK_NAME = "scrape"


class RunLocked(RuntimeError):
    """Another run is already in progress."""


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def open_client(settings=None):
    """Build the transport named by SCRAPER_TRANSPORT."""
    settings = settings or get_settings()
    if settings.transport == "selenium":
        return SeleniumClient(settings)

    from .client import ViewpointClient

    return ViewpointClient(settings)


def refresh_api_index(settings: Optional[Settings] = None) -> bool:
    """Ask Acres-API to rebuild its catalogue now that the data has moved.

    The API caches its index for ten minutes, so without this a listing sold
    minutes ago keeps showing as for sale. A failure here is not a failed run:
    the cache expires on its own.
    """
    return _post_acres_api("/api/index/refresh", settings=settings, timeout=30)


def dispatch_notifications(settings: Optional[Settings] = None) -> bool:
    """Ask Acres-API to send favourite-change and inactivity pushes.

    Called after every run, even when nothing listed changed, so the 3-day
    inactivity nudge still has a chance to fire.
    """
    return _post_acres_api("/api/notifications/dispatch", settings=settings, timeout=60)


def _post_acres_api(
    path: str,
    settings: Optional[Settings] = None,
    timeout: int = 30,
) -> bool:
    settings = settings or get_settings()
    if not settings.acres_api_url:
        return False

    url = f"{settings.acres_api_url}{path}"
    headers = {}
    if settings.scraper_api_secret:
        headers["X-Scraper-Secret"] = settings.scraper_api_secret
    try:
        response = requests.post(url, headers=headers, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Could not reach Acres-API at %s: %s", url, exc)
        return False

    log.info("Called Acres-API %s", path)
    return True


def log_summary(summary: RunSummary) -> None:
    """One greppable line describing the whole run."""
    document = summary.to_document()
    fields = " ".join(
        f"{key}={value}"
        for key, value in (
            ("seen", document["listings_seen"]),
            ("new", document["new_listings"]),
            ("price", document["price_changes"]),
            ("sold", document["sold"]),
            ("pending", document.get("pending", 0)),
            ("delisted", document["delisted"]),
            ("relisted", document["relisted"]),
            ("unchanged", document["unchanged"]),
            ("skipped", document["skipped"]),
            ("fetches", document["detail_fetches"]),
            ("stale_rechecked", document["stale_rechecked"]),
            ("errors", len(document["errors"])),
            ("seconds", f"{document['duration_seconds']:.1f}"),
        )
    )
    log.info("run complete %s", fields)


def scrape(
    limit: Optional[int] = None,
    since: Optional[Any] = None,
    store: Optional[Store] = None,
    use_lock: bool = True,
) -> RunSummary:
    """Scrape the activity feed and save what changed.

    Raises RunLocked when another run holds the lock.
    """
    from .sync import sync, sync_new_today

    settings = get_settings()
    owns_store = store is None
    if owns_store:
        store = Store(connect(settings), settings)

    try:
        store.ensure_indexes()

        if use_lock and not store.acquire_lock(LOCK_NAME, settings.run_lock_ttl_seconds):
            raise RunLocked("another scraper run is already in progress")

        started_at = utcnow()
        try:
            with open_client(settings) as client:
                if settings.transport == "selenium":
                    summary = sync_new_today(client, store, limit=limit)
                else:
                    summary = sync(client, store, since=since, limit=limit)
        except Exception as exc:
            # A crashed run is the one most worth having a record of.
            failed = RunSummary(started_at=started_at, finished_at=utcnow())
            failed.errors.append(f"run failed: {exc}")
            store.record_run(failed)
            raise
        finally:
            if use_lock:
                store.release_lock(LOCK_NAME)

        store.record_run(summary)
        log_summary(summary)

        if summary.writes:
            refresh_api_index(settings)
        dispatch_notifications(settings)

        return summary
    finally:
        if owns_store:
            store.client.close()


def main(argv: Optional[List[str]] = None) -> int:
    configure_logging()
    argv = argv if argv is not None else sys.argv[1:]

    limit: Optional[int] = None
    if "--limit" in argv:
        index = argv.index("--limit")
        if index + 1 < len(argv):
            limit = int(argv[index + 1])

    settings = get_settings()
    if not settings.viewpoint_user or not settings.viewpoint_pass:
        log.error("VIEWPOINT_USER and VIEWPOINT_PASS must be set")
        return 1

    log.info(
        "Starting run: transport=%s database=%s limit=%s",
        settings.transport,
        settings.mongodb_db_name,
        limit,
    )

    try:
        scrape(limit=limit, use_lock="--no-lock" not in argv)
    except RunLocked as exc:
        # Cron firing while the previous run is still going is expected, not a
        # failure, so it must not look like one to whatever watches exit codes.
        log.warning("Skipping this run: %s", exc)
        return 0
    except Exception:
        log.exception("Scraper run failed")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
