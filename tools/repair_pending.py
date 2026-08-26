"""Restore pending listings that were archived as sold.

Viewpoint status 6 is sold-subject-to-conditions. Overlay text containing
"Sold" was stored as SOLD and moved into sold_properties. Those listings are
still pending and belong in properties.

    python -m tools.repair_pending          # report what would move
    python -m tools.repair_pending --yes    # move them and refresh the API index
"""

import argparse
import logging
import pathlib
import sys
from typing import List, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper.backfill import restore_misarchived_pending  # noqa: E402
from scraper.config import get_settings  # noqa: E402
from scraper.run import configure_logging, refresh_api_index  # noqa: E402
from scraper.store import Store, connect  # noqa: E402

log = logging.getLogger("repair_pending")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually write the changes")
    args = parser.parse_args(argv)

    configure_logging()
    settings = get_settings()
    store = Store(connect(settings), settings)

    log.info("Database: %s", store.db.name)
    dry_run = not args.yes
    if dry_run:
        log.info("Dry run. Nothing will be written. Re-run with --yes to apply.")

    try:
        summary = restore_misarchived_pending(store, dry_run=dry_run)
        log.info("Pending restore: %s", summary)
        if args.yes and summary["restored"]:
            refresh_api_index(settings)
        return 0
    finally:
        store.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
