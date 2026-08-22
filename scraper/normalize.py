"""Turn raw viewpoint data into the document shape the app, web, and API read.

Field names and their casing are load-bearing: Acres-API projects on ``Status``,
``Price``, ``PID`` and the uppercase MLS block, and both clients read the same
keys directly. Nothing here may rename an existing key.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .identity import listing_id_from_url, sequence_from_url

STATUS_FOR_SALE = "FOR SALE"
STATUS_PENDING = "PENDING SALE"
STATUS_SOLD = "SOLD"

# Acres-API decides map visibility with a substring check for
# sold/sale/active/list/offer (is_sale_or_sold) and colours lots the same way.
# Raw viewpoint values like "NEW PRICE" and "PENDING" contain none of those
# tokens, so every such listing is silently dropped from the map. A price change
# is an event, not a status, so it is recorded on the document's timestamps and
# in recent_updates instead of being conflated with the listing's state.
_SOLD_TOKENS = ("sold", "closed")
_PENDING_TOKENS = ("pending", "conditional", "offer", "under contract")

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
    if any(token in text for token in _PENDING_TOKENS):
        return STATUS_PENDING
    return STATUS_FOR_SALE


def is_sold_status(value: Any) -> bool:
    return normalize_status(value) == STATUS_SOLD


def utcnow() -> datetime:
    """Naive UTC, matching what Acres-API's parse_datetime expects."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
        doc["listing_sequence"] = str(doc.get("listing_sequence") or sequence_from_url(resolved_url))

    raw_status = doc.get("Status")
    doc["Status"] = normalize_status(raw_status)
    if raw_status is not None and str(raw_status).strip():
        doc["status_raw"] = str(raw_status).strip().upper()

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

    return doc
