"""Address geocoding, used only when viewpoint does not supply coordinates.

Nominatim allows roughly one request per second, so every lookup is cached in
MongoDB by normalized address. An address is therefore geocoded at most once,
including its failures, which stops repeated runs from re-requesting the same
addresses that will never resolve.
"""

import re
import time
from typing import Any, Dict, Optional

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "AcresApp/1.0 (property listing aggregator; contact via myacresapp.com)"

# Nova Scotia, generously bounded. A result outside this is a bad match.
NS_LAT_MIN, NS_LAT_MAX = 43.4, 47.0
NS_LON_MIN, NS_LON_MAX = -66.4, -59.7

MIN_REQUEST_INTERVAL = 1.1

_last_request_at = 0.0


def normalize_address_key(address: str) -> str:
    """Collapse an address into a stable cache key."""
    return re.sub(r"[^a-z0-9]+", " ", str(address or "").lower()).strip()


def within_nova_scotia(latitude: float, longitude: float) -> bool:
    return NS_LAT_MIN <= latitude <= NS_LAT_MAX and NS_LON_MIN <= longitude <= NS_LON_MAX


def address_variants(address: str) -> list:
    """Progressively looser queries, most specific first."""
    address = str(address or "").strip()
    if not address:
        return []

    variants = [f"{address}, Nova Scotia, Canada", address]
    if "," in address:
        town = address.split(",")[-1].strip()
        if town:
            variants.append(f"{town}, Nova Scotia, Canada")
    return variants


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def geocode_address(address: str, session: Optional[requests.Session] = None) -> Optional[Dict[str, Any]]:
    """Look up an address with Nominatim. Returns None when nothing matches."""
    variants = address_variants(address)
    if not variants:
        return None

    http = session or requests
    for query in variants:
        _throttle()
        try:
            response = http.get(
                NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": 1, "countrycodes": "ca"},
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            if response.status_code != 200:
                continue
            results = response.json()
        except (requests.RequestException, ValueError):
            continue

        if not results:
            continue

        try:
            latitude = float(results[0].get("lat"))
            longitude = float(results[0].get("lon"))
        except (TypeError, ValueError):
            continue

        if not within_nova_scotia(latitude, longitude):
            continue

        return {
            "latitude": latitude,
            "longitude": longitude,
            "display_name": results[0].get("display_name", ""),
        }

    return None


class CachedGeocoder:
    """Geocoder backed by a MongoDB cache collection."""

    def __init__(self, cache_collection=None, session: Optional[requests.Session] = None):
        self.cache = cache_collection
        self.session = session

    def lookup(self, address: str) -> Optional[Dict[str, Any]]:
        key = normalize_address_key(address)
        if not key:
            return None

        if self.cache is not None:
            cached = self.cache.find_one({"_id": key})
            if cached is not None:
                if cached.get("latitude") is None:
                    return None
                return {
                    "latitude": cached["latitude"],
                    "longitude": cached["longitude"],
                    "display_name": cached.get("display_name", ""),
                }

        result = geocode_address(address, session=self.session)

        if self.cache is not None:
            self.cache.update_one(
                {"_id": key},
                {
                    "$set": {
                        "address": address,
                        "latitude": result["latitude"] if result else None,
                        "longitude": result["longitude"] if result else None,
                        "display_name": result.get("display_name", "") if result else "",
                    }
                },
                upsert=True,
            )

        return result
