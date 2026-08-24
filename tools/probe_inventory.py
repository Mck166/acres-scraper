"""Probe Viewpoint for a full-market list/search/map endpoint.

    python -m tools.probe_inventory

Logs in with VIEWPOINT_USER / VIEWPOINT_PASS, tries listing/search, list, map,
and results, captures the authenticated /map SPA's network calls, and writes
output/debug/inventory_probe.json with the enumerator the scanner should use.
"""

import logging
import pathlib
import sys
from typing import List, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper.client import ViewpointClient  # noqa: E402
from scraper.config import get_settings  # noqa: E402
from scraper.inventory import probe  # noqa: E402
from scraper.run import configure_logging  # noqa: E402

log = logging.getLogger("probe_inventory")


def main(argv: Optional[List[str]] = None) -> int:
    configure_logging()
    args = argv if argv is not None else sys.argv[1:]
    capture_map = "--no-map" not in args

    settings = get_settings()
    if not settings.viewpoint_user or not settings.viewpoint_pass:
        log.error("VIEWPOINT_USER and VIEWPOINT_PASS must be set")
        return 1

    with ViewpointClient(settings) as client:
        result = probe(client, capture_map=capture_map)

    chosen = result.get("chosen") or {}
    log.info("Chosen: %s", chosen.get("reason") or chosen)
    ok_attempts = [
        attempt
        for attempt in result.get("attempts") or []
        if attempt.get("ok") and attempt.get("listing_count")
    ]
    for attempt in sorted(ok_attempts, key=lambda item: item.get("active_count") or 0, reverse=True)[:8]:
        log.info(
            "  %s -> %s listings (%s active)",
            attempt.get("label"),
            attempt.get("listing_count"),
            attempt.get("active_count"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
