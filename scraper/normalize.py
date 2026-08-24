"""Turn raw viewpoint data into the document shape the app, web, and API read.

Field names and their casing are load-bearing: Acres-API projects on ``Status``,
``Price``, ``PID`` and the uppercase MLS block, and both clients read the same
keys directly. Nothing here may rename an existing key.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from .identity import class_id_from_url, listing_id_from_url

log = logging.getLogger(__name__)

STATUS_FOR_SALE = "FOR SALE"
STATUS_PENDING = "PENDING SALE"
STATUS_SOLD = "SOLD"
STATUS_EXPIRED = "EXPIRED"

ATLANTIC = ZoneInfo("America/Halifax")

# Viewpoint's numeric listing states, learned from its new-today change events
# and confirmed against the status each cutsheet displays: a listing goes
# None -> 5 when it lists, 5 -> 6 when it sells subject to conditions,
# 6 -> 2 when that sale closes, and 5 -> 1 when it comes off the market unsold.
STATUS_IDS = {
    "1": STATUS_EXPIRED,   # Expired
    "2": STATUS_SOLD,      # Sold, closed
    "3": STATUS_EXPIRED,   # Cancelled
    "5": STATUS_FOR_SALE,  # Active
    "6": STATUS_PENDING,   # Sold subject to conditions
    "7": STATUS_FOR_SALE,  # Newly listed, price not yet published
    "8": STATUS_EXPIRED,   # Withdrawn
}

EVENT_PRICE = "1"
EVENT_STATUS = "2"
STATUS_ID_PENDING = "6"
STATUS_ID_SOLD = "2"

EVENT_DATE_FIELDS = (
    "listed_on",
    "sold_on",
    "pending_on",
    "price_changed_on",
    "closes_on",
    "source_updated_at",
)


def status_from_id(status_id: Any) -> Optional[str]:
    """Map a viewpoint status id onto our vocabulary.

    Returns None for ids we have not seen, so the caller can fall back to the
    status the page displays rather than guess that a listing is for sale.
    """
    if status_id is None:
        return None

    key = str(status_id).strip()
    status = STATUS_IDS.get(key)
    if status is None:
        log.warning("Unmapped viewpoint status id %r; falling back to the page's status text", key)
    return status

# Acres-API decides map visibility with a substring check for
# sold/sale/active/list/offer (is_sale_or_sold) and colours lots the same way.
# Raw viewpoint values like "NEW PRICE" and "PENDING" contain none of those
# tokens, so every such listing is silently dropped from the map. A price change
# is an event, not a status, so it is recorded on the document's timestamps and
# in recent_updates instead of being conflated with the listing's state.
_SOLD_TOKENS = ("sold", "closed")
_PENDING_TOKENS = ("pending", "conditional", "offer", "under contract")
_EXPIRED_TOKENS = ("expired", "withdrawn", "cancelled", "canceled", "terminated")

_PRICE_CLEAN_RE = re.compile(r"[^0-9.]")


def parse_price(value: Any) -> Optional[float]:
    """Extract a number from a price that may be a string, int, or float."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) or None
    text = _PRICE_CLEAN_RE.sub("", str(value))
    if not text or text == ".":
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed or None


def format_price(value: Any) -> str:
    """Render a price the way the clients expect to display it."""
    parsed = parse_price(value)
    if parsed is None:
        return ""
    return f"${parsed:,.0f}"


def normalize_status(value: Any) -> str:
    """Map a raw viewpoint status onto the vocabulary Acres-API understands."""
    text = str(value or "").strip().lower()
    if not text:
        return STATUS_FOR_SALE
    if any(token in text for token in _SOLD_TOKENS):
        return STATUS_SOLD
    if any(token in text for token in _EXPIRED_TOKENS):
        return STATUS_EXPIRED
    if any(token in text for token in _PENDING_TOKENS):
        return STATUS_PENDING
    return STATUS_FOR_SALE


def is_sold_status(value: Any) -> bool:
    return normalize_status(value) == STATUS_SOLD


def is_pending_status(value: Any) -> bool:
    return normalize_status(value) == STATUS_PENDING


def is_off_market_status(value: Any) -> bool:
    """True when a listing should no longer appear as available."""
    return normalize_status(value) in (STATUS_SOLD, STATUS_EXPIRED)


def utcnow() -> datetime:
    """Naive UTC, matching what Acres-API's parse_datetime expects."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_viewpoint_datetime(value: Any) -> Optional[datetime]:
    """Parse a Viewpoint timestamp as America/Halifax wall time.

    The site sends naive strings. We keep them naive so Mongo and the API can
    compare calendar dates in Atlantic Time without a timezone round-trip.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(ATLANTIC).replace(tzinfo=None)
        return value

    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def atlantic_today(now: Optional[datetime] = None):
    """Today's calendar date in America/Halifax."""
    if now is None:
        current = datetime.now(ATLANTIC)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=ATLANTIC)
    else:
        current = now.astimezone(ATLANTIC)
    return current.date()


def date_is_today(value: Any, now: Optional[datetime] = None) -> bool:
    parsed = parse_viewpoint_datetime(value)
    if parsed is None:
        return False
    return parsed.date() == atlantic_today(now)


def apply_listing_events(document: Dict[str, Any], events: Optional[Iterable[Dict[str, Any]]]) -> Dict[str, Any]:
    """Copy price and status events onto the listing document."""
    history = list(document.get("price_history") or [])
    for event in events or []:
        event_id = str(event.get("event_id") or "")
        when = parse_viewpoint_datetime(event.get("event_time") or event.get("created"))
        if event_id == EVENT_PRICE:
            if when is not None:
                document["price_changed_on"] = when
            old_price = parse_price(event.get("oldvalue"))
            history.append(
                {
                    "price": format_price(old_price) if old_price is not None else event.get("oldvalue"),
                    "price_value": old_price,
                    "date": when,
                }
            )
        elif event_id == EVENT_STATUS:
            newvalue = str(event.get("newvalue") or "").strip()
            if newvalue == STATUS_ID_PENDING and when is not None:
                document["pending_on"] = when
            elif newvalue == STATUS_ID_SOLD and when is not None:
                document["sold_on"] = when
    if history:
        document["price_history"] = history
    return document


DEFAULT_LOOKBACK_HOURS = 24


def as_since(value: Any = None) -> str:
    """Render a moment as the Unix timestamp viewpoint's activity feed wants."""
    if value is None:
        value = utcnow() - timedelta(hours=DEFAULT_LOOKBACK_HOURS)

    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return str(int(moment.timestamp()))

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value))

    return str(value)


def coerce_coordinate(value: Any) -> Optional[float]:
    """Accept a coordinate only if it is a real, non-zero number."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number == 0:
        return None
    return number


def normalize_document(raw: Dict[str, Any], url: Optional[str] = None) -> Dict[str, Any]:
    """Normalize a scraped property into its stored form.

    Unknown keys are preserved untouched so MLS detail fields keep flowing
    through to the clients without needing to be enumerated here.
    """
    doc: Dict[str, Any] = dict(raw or {})

    resolved_url = url or doc.get("url")
    if resolved_url:
        doc["url"] = resolved_url

    listing_id = doc.get("listing_id") or listing_id_from_url(resolved_url)
    if listing_id:
        doc["listing_id"] = str(listing_id)
        doc["listing_class_id"] = str(doc.get("listing_class_id") or class_id_from_url(resolved_url))

    raw_status = str(doc.get("Status") or "").strip().upper()
    doc["Status"] = normalize_status(raw_status)
    # Keep the site's own wording only when it says something the normalized
    # value does not. Re-normalizing a document must not overwrite it with the
    # value this function itself produced.
    if raw_status and raw_status != doc["Status"] and "status_raw" not in doc:
        doc["status_raw"] = raw_status

    price_value = parse_price(doc.get("Price"))
    doc["price_value"] = price_value
    if price_value is not None:
        doc["Price"] = format_price(price_value)

    pid = doc.get("PID")
    if pid is not None:
        doc["PID"] = str(pid).strip() or None

    doc["latitude"] = coerce_coordinate(doc.get("latitude"))
    doc["longitude"] = coerce_coordinate(doc.get("longitude"))

    photos = doc.get("Photos") or []
    if not isinstance(photos, list):
        photos = []
    doc["Photos"] = photos
    doc["Photo_Count"] = len(photos)

    for field in EVENT_DATE_FIELDS:
        if field in doc:
            parsed = parse_viewpoint_datetime(doc.get(field))
            if parsed is not None:
                doc[field] = parsed
            elif not doc.get(field):
                doc.pop(field, None)

    if is_pending_status(doc.get("Status")) and not doc.get("pending_on"):
        pending = parse_viewpoint_datetime(doc.get("status_changed_on") or doc.get("status_dt"))
        if pending is not None:
            doc["pending_on"] = pending

    return doc
