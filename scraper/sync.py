"""The sync engine: read the activity feed, work out what changed, save it.

The feed reports every listing that changed, with its current price, status and
coordinates already attached. That is enough to classify each entry against what
we hold without fetching anything, so a full page is only pulled for listings
that are genuinely new or genuinely repriced. A run that finds nothing new
therefore writes nothing at all.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .geocode import CachedGeocoder
from .normalize import (
    STATUS_EXPIRED,
    STATUS_ID_PENDING,
    STATUS_ID_SOLD,
    STATUS_PENDING,
    STATUS_SOLD,
    apply_listing_events,
    normalize_document,
    normalize_status,
    parse_price,
    status_from_id,
    utcnow,
)
from .store import (
    CHANGE_DELISTED,
    CHANGE_NEW,
    CHANGE_NONE,
    CHANGE_PENDING,
    CHANGE_PRICE,
    CHANGE_SOLD,
    RunSummary,
    SaveResult,
    Store,
)

log = logging.getLogger(__name__)


@dataclass
class Change:
    """One listing's classification, decided before anything is fetched."""

    listing_id: str
    url: str
    kind: str
    status: str
    old_price: Optional[float] = None
    new_price: Optional[float] = None
    sold_price: Optional[float] = None
    existing: Optional[dict] = None
    entry: Dict[str, Any] = field(default_factory=dict)

    @property
    def needs_detail(self) -> bool:
        """Whether this change requires pulling the listing's full page.

        A listing we already hold carries everything needed to retire it, so a
        sale or a delisting costs no request. Anything new, repriced, or newly
        pending is fetched so the MLS event dates land on the document.
        """
        if self.kind in (CHANGE_NEW, CHANGE_PRICE, CHANGE_PENDING):
            return True
        if self.kind == CHANGE_SOLD:
            return self.existing is None
        return False


def entry_status(entry: Dict[str, Any]) -> str:
    """The listing's current state, preferring the numeric id over free text."""
    return status_from_id(entry.get("status_id")) or normalize_status(entry.get("status"))


def classify(
    entry: Dict[str, Any],
    active: Optional[dict],
    archived: Optional[dict],
) -> Change:
    """Decide what, if anything, an activity entry means for our data."""
    listing_id = str(entry.get("listing_id") or "")
    status = entry_status(entry)
    new_price = parse_price(entry.get("list_price"))
    sold_price = parse_price(entry.get("sold_price"))

    def change(kind: str, old_price: Optional[float] = None) -> Change:
        return Change(
            listing_id=listing_id,
            url=entry.get("url", ""),
            kind=kind,
            status=status,
            old_price=old_price,
            new_price=new_price,
            sold_price=sold_price,
            existing=active,
            entry=entry,
        )

    if archived is not None:
        # Already retired. Only a return to the market is worth acting on.
        if status in (STATUS_SOLD, STATUS_EXPIRED):
            return change(CHANGE_NONE)
        return change(CHANGE_NEW)

    if active is None:
        if status == STATUS_EXPIRED:
            # Never seen it and it is off the market; there is nothing to record.
            return change(CHANGE_NONE)
        if status == STATUS_SOLD:
            # A sale of a listing we never held still belongs in the archive,
            # which is how the sold history fills out over time.
            return change(CHANGE_SOLD)
        return change(CHANGE_NEW)

    old_price = parse_price(active.get("Price"))

    if status == STATUS_SOLD:
        return change(CHANGE_SOLD, old_price)
    if status == STATUS_EXPIRED:
        return change(CHANGE_DELISTED, old_price)
    if status == STATUS_PENDING and normalize_status(active.get("Status")) != STATUS_PENDING:
        return change(CHANGE_PENDING, old_price)
    if new_price is not None and old_price is not None and new_price != old_price:
        return change(CHANGE_PRICE, old_price)

    return change(CHANGE_NONE, old_price)


def plan_changes(activity: Iterable[Dict[str, Any]], store: Store) -> List[Change]:
    """Classify a whole activity feed against what is already stored."""
    active = store.active_snapshot()
    archived = store.archived_snapshot()

    changes = []
    for entry in activity:
        listing_id = str(entry.get("listing_id") or "")
        if not listing_id:
            continue
        changes.append(classify(entry, active.get(listing_id), archived.get(listing_id)))
    return changes


# -- applying a change ---------------------------------------------------


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


def merge_activity_dates(document: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
    """Fill event dates from the activity feed when the cutsheet omitted them."""
    if entry.get("status_id") is not None and not document.get("status_id"):
        document["status_id"] = str(entry["status_id"]).strip()

    if entry.get("list_dt") and not document.get("listed_on"):
        document["listed_on"] = entry["list_dt"]

    status_id = str(entry.get("status_id") or document.get("status_id") or "")
    if status_id == STATUS_ID_SOLD:
        if entry.get("sold_dt") and not document.get("sold_on"):
            document["sold_on"] = entry["sold_dt"]
    elif status_id == STATUS_ID_PENDING:
        if entry.get("status_dt") and not document.get("pending_on"):
            document["pending_on"] = entry["status_dt"]

    apply_listing_events(document, entry.get("events") or [])
    return document


def build_document(
    client,
    change: Change,
    geocoder: Optional[CachedGeocoder] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch and normalize a listing's full record."""
    raw = client.fetch_listing(change.url)
    if not raw:
        return None

    merge_activity_dates(raw, change.entry)
    document = normalize_document(raw, url=change.url)
    return enrich_coordinates(document, geocoder)


def apply_change(
    client,
    store: Store,
    change: Change,
    summary: RunSummary,
    geocoder: Optional[CachedGeocoder] = None,
    counts_as_seen: bool = True,
) -> Optional[SaveResult]:
    """Carry out one classified change."""
    if change.kind == CHANGE_NONE:
        summary.skipped += 1
        return None

    if not change.needs_detail and change.existing is not None:
        # Classification runs off a projection holding only the fields a diff
        # needs, so the whole document is read before archiving; archiving
        # rewrites it, and everything absent from the projection would be lost.
        existing = store.find_active(change.listing_id, change.url) or change.existing
        reason = "sold" if change.kind == CHANGE_SOLD else "expired"
        result = store.archive_listing(existing, reason=reason, sold_price=change.sold_price)
        summary.record(result, seen=counts_as_seen)
        store.record_change(result, existing)
        log.info("%s %s (no fetch needed)", change.listing_id, result.action)
        return result

    try:
        document = build_document(client, change, geocoder)
        summary.detail_fetches += 1
    except Exception as exc:
        log.error("Failed to fetch %s: %s", change.url, exc)
        summary.errors.append(f"fetch {change.url}: {exc}")
        return None

    if not document:
        summary.errors.append(f"empty listing at {change.url}")
        return None

    try:
        result = store.save_listing(document)
        store.record_change(result, document)
    except Exception as exc:
        log.error("Failed to save %s: %s", change.url, exc)
        summary.errors.append(f"save {change.url}: {exc}")
        return None

    summary.record(result, seen=counts_as_seen)
    log.info("%s %s", change.listing_id, result.action)
    return result


# -- the run ------------------------------------------------------------


def stale_recheck(
    client,
    store: Store,
    summary: RunSummary,
    limit: int,
    geocoder: Optional[CachedGeocoder] = None,
    skip: Optional[Iterable[str]] = None,
) -> None:
    """Re-check the active listings we have looked at least recently.

    The activity feed reports delistings, but only for listings that generate an
    event. A listing that quietly stops being updated would otherwise sit in the
    app as available forever, so each run revisits a bounded batch of our own
    oldest records.
    """
    if limit <= 0:
        return

    already_seen = set(skip or ())

    for doc in store.stale_active_listings(limit):
        url = doc.get("url")
        if not url:
            continue
        if str(doc.get("listing_id") or "") in already_seen:
            # The feed already told us about this one a moment ago.
            continue

        change = Change(
            listing_id=str(doc.get("listing_id") or ""),
            url=url,
            kind=CHANGE_NEW,  # forces a fetch; save_listing decides what really changed
            status=normalize_status(doc.get("Status")),
            existing=doc,
        )

        result = apply_change(client, store, change, summary, geocoder, counts_as_seen=False)
        summary.stale_rechecked += 1
        if result is not None and result.action != CHANGE_NONE:
            log.info("stale re-check found %s on %s", result.action, url)


def sync(
    client,
    store: Store,
    since: Any = None,
    limit: Optional[int] = None,
    stale_limit: Optional[int] = None,
    activity: Optional[Iterable[Dict[str, Any]]] = None,
) -> RunSummary:
    """Bring MongoDB in line with the activity feed."""
    summary = RunSummary()
    store.ensure_indexes()

    geocoder = CachedGeocoder(store.geocode_cache)

    entries = list(activity) if activity is not None else list(client.new_today_activity(since))
    if limit is not None:
        entries = entries[:limit]

    changes = plan_changes(entries, store)
    actionable = [change for change in changes if change.kind != CHANGE_NONE]
    log.info(
        "Activity feed: %d listings, %d actionable, %d needing a full fetch",
        len(changes),
        len(actionable),
        sum(1 for change in actionable if change.needs_detail),
    )

    for change in changes:
        apply_change(client, store, change, summary, geocoder)

    if stale_limit is None:
        stale_limit = store.settings.stale_recheck_limit
    stale_recheck(
        client,
        store,
        summary,
        stale_limit,
        geocoder,
        skip={change.listing_id for change in changes if change.listing_id},
    )

    summary.finished_at = utcnow()
    return summary


# Kept so the Selenium transport, which only knows how to list URLs, still works.
def sync_new_today(client, store: Store, limit: Optional[int] = None) -> RunSummary:
    """Scrape listing pages directly, without the activity feed's metadata."""
    summary = RunSummary()
    store.ensure_indexes()
    geocoder = CachedGeocoder(store.geocode_cache)

    urls = list(client.new_today_urls())
    if limit is not None:
        urls = urls[:limit]

    log.info("Processing %d listings from the new-today list", len(urls))

    for url in urls:
        change = Change(listing_id="", url=url, kind=CHANGE_NEW, status="")
        apply_change(client, store, change, summary, geocoder)

    summary.finished_at = utcnow()
    return summary
