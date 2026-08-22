"""Phase 3 gate: normalization must not break what the clients read.

The field names below were taken from the three consumers rather than guessed:
Acres-API's INDEX_PROJECTION and alias tuples, the app's PropertyDetailScreen
and card components, and the website's lib/properties.ts helpers and detail
page. Casing is part of the contract, so the assertions are on literal names.
"""

import json
import pathlib
from datetime import datetime

import pytest

from scraper.normalize import (
    STATUS_EXPIRED,
    STATUS_FOR_SALE,
    STATUS_PENDING,
    STATUS_SOLD,
    as_since,
    coerce_coordinate,
    format_price,
    is_off_market_status,
    is_sold_status,
    normalize_document,
    normalize_status,
    parse_price,
    status_from_id,
)

GOLDEN_DIR = pathlib.Path(__file__).parent / "fixtures" / "golden"

# Read by Acres-API for the map and feed index, and by both clients on cards.
CORE_FIELDS = {
    "PID",
    "url",
    "Price",
    "Status",
    "Address",
    "Photos",
    "Photo_Count",
    "latitude",
    "longitude",
}

# Read by the app's PropertyDetailScreen and the website's detail page.
DETAIL_FIELDS = {
    "AGE",
    "APPLIANCES INCL.",
    "BASEMENT",
    "BATHROOMS (F/H)",
    "BEDS",
    "BUILDING DIMENSIONS",
    "BUILDING STYLE",
    "DRINKING WATER",
    "EXCLUSIONS",
    "EXTERIOR",
    "FLOORING",
    "FOUNDATION",
    "FUEL SUPPLY",
    "HAS GARAGE",
    "HEATING/COOLING",
    "INCLUSIONS",
    "LAND FEATURES",
    "LISTED BY",
    "MAIN LIVING AREA",
    "PARKING",
    "PROPERTY FEATURES",
    "ROOF",
    "SEWER",
    "TOTAL LIVING AREA",
    "TYPE",
    "UTILITIES",
    "WATERFRONT",
}

CLIENT_FIELDS = CORE_FIELDS | DETAIL_FIELDS

# Normalization deliberately rewrites these; every other key must pass through
# with its value untouched.
REWRITTEN_FIELDS = {"Status", "Price", "Photo_Count", "latitude", "longitude", "PID"}


def golden_names():
    return [path.stem for path in sorted(GOLDEN_DIR.glob("*.json"))]


@pytest.fixture(params=golden_names())
def golden(request):
    return json.loads((GOLDEN_DIR / f"{request.param}.json").read_text())


# -- the round trip ------------------------------------------------------


def test_normalization_keeps_every_key_a_client_reads(golden):
    normalized = normalize_document(golden)

    for field in sorted(CLIENT_FIELDS):
        if field in golden:
            assert field in normalized, f"{field} was dropped"


def test_normalization_never_drops_or_renames_a_key(golden):
    normalized = normalize_document(golden)
    assert set(golden) <= set(normalized), f"lost keys: {sorted(set(golden) - set(normalized))}"


def test_normalization_leaves_untouched_fields_byte_identical(golden):
    normalized = normalize_document(golden)

    for field, value in golden.items():
        if field in REWRITTEN_FIELDS:
            continue
        assert normalized[field] == value, f"{field} changed"


def test_normalization_is_stable_when_applied_twice(golden):
    once = normalize_document(golden)
    twice = normalize_document(once)
    assert once == twice


# -- price ---------------------------------------------------------------


def test_price_keeps_its_display_form_and_gains_a_number(golden):
    normalized = normalize_document(golden)

    if golden.get("Price"):
        assert normalized["Price"].startswith("$")
        assert normalized["price_value"] == parse_price(golden["Price"])
    else:
        assert normalized["price_value"] is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("$329,900", 329900.0),
        ("329900", 329900.0),
        (329900, 329900.0),
        (329900.5, 329900.5),
        ("$1,020,000", 1020000.0),
        ("", None),
        (None, None),
        ("N/A", None),
        ("Not Yet Closed", None),
        (0, None),
    ],
)
def test_parse_price(value, expected):
    assert parse_price(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(329900, "$329,900"), ("$1,020,000", "$1,020,000"), (None, ""), ("N/A", "")],
)
def test_format_price(value, expected):
    assert format_price(value) == expected


# -- status --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("FOR SALE", STATUS_FOR_SALE),
        ("Sold", STATUS_SOLD),
        ("SOLD", STATUS_SOLD),
        ("Expired", STATUS_EXPIRED),
        ("Cancelled", STATUS_EXPIRED),
        ("Pending", STATUS_PENDING),
        ("", STATUS_FOR_SALE),
        (None, STATUS_FOR_SALE),
    ],
)
def test_normalize_status(raw, expected):
    assert normalize_status(raw) == expected


def test_new_price_becomes_for_sale():
    """A price cut is an event, not a state.

    Acres-API decides map visibility by looking for sold/sale/active/list/offer
    in the status text. "NEW PRICE" contains none of them, so every repriced
    listing was being dropped from the map.
    """
    assert normalize_status("NEW PRICE") == STATUS_FOR_SALE
    assert normalize_document({"Status": "NEW PRICE"})["Status"] == STATUS_FOR_SALE


def test_normalized_statuses_survive_the_api_substring_check():
    """Mirrors Acres-API's is_sale_or_sold, which decides what the map shows."""

    def visible_on_map(status: str) -> bool:
        text = status.strip().lower()
        if not text:
            return True
        if "sold" in text:
            return True
        return any(token in text for token in ("sale", "active", "list", "offer"))

    assert visible_on_map(STATUS_FOR_SALE)
    assert visible_on_map(STATUS_SOLD)
    assert visible_on_map(STATUS_PENDING)
    assert not visible_on_map(STATUS_EXPIRED), "expired listings must not reach the map"


def test_raw_status_is_kept_for_reference():
    assert normalize_document({"Status": "NEW PRICE"})["status_raw"] == "NEW PRICE"


@pytest.mark.parametrize(
    "status_id,expected",
    [
        ("1", STATUS_EXPIRED),
        ("2", STATUS_SOLD),
        ("3", STATUS_EXPIRED),
        ("5", STATUS_FOR_SALE),
        ("6", STATUS_SOLD),
        (5, STATUS_FOR_SALE),
        ("999", None),
        (None, None),
    ],
)
def test_status_from_id(status_id, expected):
    assert status_from_id(status_id) == expected


def test_sold_and_off_market_helpers():
    assert is_sold_status("Sold")
    assert not is_sold_status("FOR SALE")
    assert is_off_market_status("Expired")
    assert is_off_market_status("Sold")
    assert not is_off_market_status("FOR SALE")


# -- coordinates ---------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("46.10131396", 46.10131396),
        (-60.749, -60.749),
        ("", None),
        (None, None),
        ("0", None),
        (0, None),
        ("not a number", None),
    ],
)
def test_coerce_coordinate(value, expected):
    assert coerce_coordinate(value) == expected


def test_coordinates_arrive_as_strings_from_the_api():
    normalized = normalize_document({"latitude": "46.10131396", "longitude": "-60.74911817"})
    assert normalized["latitude"] == 46.10131396
    assert normalized["longitude"] == -60.74911817


# -- photos --------------------------------------------------------------


def test_photo_count_always_matches_the_list(golden):
    normalized = normalize_document(golden)
    assert normalized["Photo_Count"] == len(normalized["Photos"])


def test_photo_count_is_corrected_when_it_disagrees():
    normalized = normalize_document({"Photos": ["a", "b", "c"], "Photo_Count": 99})
    assert normalized["Photo_Count"] == 3


# -- identity ------------------------------------------------------------


def test_listing_id_is_derived_from_the_url(golden):
    normalized = normalize_document(golden)
    if golden.get("url"):
        assert normalized["listing_id"]
        assert normalized["listing_id"] in golden["url"]


# -- activity window -----------------------------------------------------


def test_as_since_renders_a_unix_timestamp():
    moment = datetime(2026, 8, 22, 0, 0, 0)
    assert as_since(moment) == "1787356800"
    assert as_since(1787356800) == "1787356800"
    assert as_since("1787356800") == "1787356800"


def test_as_since_defaults_to_a_lookback_window():
    """An empty since returns a stale window, so a timestamp is always sent."""
    assert as_since(None).isdigit()
    assert int(as_since(None)) > 0
