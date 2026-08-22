"""The sync engine: read the activity list, work out what changed, save it."""

import logging
from typing import Any, Dict, Iterable, List, Optional

from .geocode import CachedGeocoder
from .normalize import normalize_document, utcnow
from .store import RunSummary, Store

log = logging.getLogger(__name__)


def enrich_coordinates(document: Dict[str, Any], geocoder: Optional[CachedGeocoder]) -> Dict[str, Any]:
    """Fill in coordinates only when viewpoint did not provide them."""
    if document.get("latitude") is not None and document.get("longitude") is not None:
        return document
    if geocoder is None:
        return document

    address = document.get("Address")
    if not address:
        return document

    location = geocoder.lookup(address)
    if location:
        document["latitude"] = location["latitude"]
        document["longitude"] = location["longitude"]
        document["geocoded_address"] = location.get("display_name", "")

    return document


def process_listing(
    client,
    store: Store,
    url: str,
    summary: RunSummary,
    geocoder: Optional[CachedGeocoder] = None,
) -> None:
    """Fetch, normalize, and save a single listing."""
    try:
        raw = client.fetch_listing(url)
    except Exception as exc:
        log.error("Failed to fetch %s: %s", url, exc)
        summary.errors.append(f"fetch {url}: {exc}")
        return

    if not raw:
        summary.errors.append(f"empty listing at {url}")
        return

    document = normalize_document(raw, url=url)
    document = enrich_coordinates(document, geocoder)

    try:
        result = store.save_listing(document)
        store.record_change(result, document)
    except Exception as exc:
        log.error("Failed to save %s: %s", url, exc)
        summary.errors.append(f"save {url}: {exc}")
        return

    summary.record(result)
    log.info("%s -> %s", url, result.action)


def sync_new_today(
    client,
    store: Store,
    limit: Optional[int] = None,
    urls: Optional[Iterable[str]] = None,
) -> RunSummary:
    """Scrape the new-today activity list into MongoDB."""
    summary = RunSummary()
    store.ensure_indexes()

    geocoder = CachedGeocoder(store.geocode_cache)

    listing_urls: List[str] = list(urls) if urls is not None else list(client.new_today_urls())
    if limit is not None:
        listing_urls = listing_urls[:limit]

    log.info("Processing %d listings from the new-today list", len(listing_urls))

    for url in listing_urls:
        process_listing(client, store, url, summary, geocoder)

    summary.finished_at = utcnow()
    return summary
