"""Entry point for a scraper run."""

import logging
import sys
from typing import List, Optional

from .config import get_settings
from .selenium_client import SeleniumClient
from .store import RunSummary, Store, connect

log = logging.getLogger(__name__)


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


def scrape(limit: Optional[int] = None, store: Optional[Store] = None) -> RunSummary:
    """Scrape the new-today list and save what changed."""
    from .sync import sync_new_today

    settings = get_settings()
    owns_store = store is None
    client_context = open_client(settings)

    if owns_store:
        store = Store(connect(settings), settings)

    try:
        with client_context as client:
            return sync_new_today(client, store, limit=limit)
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

    try:
        summary = scrape(limit=limit)
    except Exception:
        log.exception("Scraper run failed")
        return 1

    log.info(
        "Run finished: %d seen, %d new, %d price changes, %d sold",
        summary.listings_seen,
        summary.new_listings,
        summary.price_changes,
        summary.sold,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
