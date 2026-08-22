"""Stable identifiers for listings and properties.

A *listing* is identified by its MLS number (``listing_id``), which changes every
time a property is relisted. A *property* is identified by its provincial parcel
ID (``PID``), which survives relisting. Keeping the two separate is what lets a
sold home reappear later as a new listing without losing its history.

Cutsheet URLs look like ``/cutsheet/{listing_id}/{class_id}``, where class id is
viewpoint's property class (1 residential, 5 land, and so on). The same pair
addresses the listing's photos, so both halves matter.
"""

import re
from typing import Optional, Tuple

CUTSHEET_RE = re.compile(r"/cutsheet/(\d+)(?:/(\d+))?", re.IGNORECASE)


def parse_cutsheet_url(url: Optional[str]) -> Optional[Tuple[str, str]]:
    """Return ``(listing_id, class_id)`` from a cutsheet URL, or None."""
    if not url:
        return None
    match = CUTSHEET_RE.search(str(url))
    if not match:
        return None
    listing_id = match.group(1)
    class_id = match.group(2) or "1"
    return listing_id, class_id


def build_cutsheet_url(listing_id: str, class_id: str = "1", base_url: str = "https://www.viewpoint.ca") -> str:
    return f"{base_url.rstrip('/')}/cutsheet/{listing_id}/{class_id}"


def listing_id_from_url(url: Optional[str]) -> Optional[str]:
    parsed = parse_cutsheet_url(url)
    return parsed[0] if parsed else None


def class_id_from_url(url: Optional[str]) -> str:
    parsed = parse_cutsheet_url(url)
    return parsed[1] if parsed else "1"


def resolve_listing_id(doc: dict) -> Optional[str]:
    """Get a document's listing id, falling back to parsing its URL."""
    if not doc:
        return None
    existing = doc.get("listing_id")
    if existing:
        return str(existing)
    return listing_id_from_url(doc.get("url"))


def resolve_pid(doc: dict) -> Optional[str]:
    if not doc:
        return None
    pid = doc.get("PID")
    if pid is None:
        return None
    pid = str(pid).strip()
    return pid or None
