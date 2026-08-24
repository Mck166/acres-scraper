"""Unit tests for market-inventory discovery helpers."""

import json
from datetime import datetime
from pathlib import Path

from scraper.inventory import (
    choose_enumerator,
    extract_cutsheet_refs,
    extract_listings,
    filter_active,
    has_atlantic_today_event,
    parse_api_request,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "viewpoint"


def test_extract_listings_from_newtoday_fixture():
    body = json.loads((FIXTURES / "newtoday.json").read_text())
    listings = extract_listings(body)
    assert len(listings) >= 10
    first = listings[0]
    assert first["listing_id"].isdigit()
    assert first["class_id"]
    assert "status_id" in first
    assert "list_dt" in first


def test_filter_active_keeps_pending_and_drops_sold():
    listings = [
        {"listing_id": "202600001", "status_id": "5"},
        {"listing_id": "202600002", "status_id": "6"},
        {"listing_id": "202600003", "status_id": "7"},
        {"listing_id": "202600004", "status_id": "2"},
        {"listing_id": "202600005", "status_id": "1"},
        {"listing_id": "202600006", "status_id": ""},
    ]
    active_ids = {item["listing_id"] for item in filter_active(listings)}
    assert active_ids == {"202600001", "202600002", "202600003", "202600006"}


def test_parse_api_request_strips_nonce():
    parsed = parse_api_request(
        "https://www.viewpoint.ca/api/v2/listing/map?CLIENT_VER=9&nonce=abc&min_lat=43.3"
    )
    assert parsed == {
        "controller": "listing",
        "method": "map",
        "params": {"min_lat": "43.3"},
        "post": False,
    }


def test_extract_cutsheet_refs():
    refs = extract_cutsheet_refs("see /cutsheet/202621159/1 and /cutsheet/202508596/5/")
    ids = {item["listing_id"]: item["class_id"] for item in refs}
    assert ids == {"202621159": "1", "202508596": "5"}


def test_choose_enumerator_tiles_when_search_is_too_big():
    chosen = choose_enumerator(
        [
            {
                "ok": False,
                "label": "GET listing/search province",
                "controller": "listing",
                "method": "search",
                "params": {},
                "post": False,
                "error": "listing/search failed: [{'message': 'Too many search results', 'code': 402}]",
                "listing_count": 0,
                "active_count": 0,
            },
            {
                "ok": True,
                "label": "GET listing/vp {}",
                "controller": "listing",
                "method": "vp",
                "params": {},
                "post": False,
                "listing_count": 665,
                "active_count": 665,
            },
        ]
    )
    assert chosen["kind"] == "search_tiles"
    assert chosen["method"] == "search"


class FakeSearchClient:
    """Splits until the tile span is small, then returns one listing per tile."""

    def call(self, controller, method, params, post=False, **_kwargs):
        from scraper.client import ViewpointError

        area = params["parameters[search_area]"]
        parts = [float(part.strip()) for part in area.split(",")]
        sw_lat, sw_lng, ne_lat, ne_lng = parts[3], parts[4], parts[5], parts[6]
        span = max(abs(ne_lat - sw_lat), abs(ne_lng - sw_lng))
        if span > 0.2:
            raise ViewpointError("listing/search failed: [{'message': 'Too many search results', 'code': 402}]")
        listing_id = str(200000000 + abs(hash((round(sw_lat, 4), round(sw_lng, 4), round(ne_lat, 4)))) % 10_000_000)
        return {
            "status": "success",
            "listings": [
                {
                    "listing_id": listing_id,
                    "class_id": "1",
                    "status_id": "5",
                    "list_dt": "2026-01-01 00:00:00",
                    "list_price": "1",
                }
            ],
        }


def test_enumerate_search_tiles_splits_oversized_cells():
    from scraper.inventory import enumerate_search_tiles

    listings = enumerate_search_tiles(
        FakeSearchClient(),
        bbox=(44.0, -64.0, 45.0, -63.0),
        min_span=0.05,
        max_tiles=80,
    )
    assert len(listings) >= 4
    assert all(item["listing_id"].isdigit() for item in listings)


def test_choose_enumerator_prefers_a_large_search_over_newtoday():
    chosen = choose_enumerator(
        [
            {
                "ok": True,
                "label": "GET listing/newtoday default",
                "controller": "listing",
                "method": "newtoday",
                "params": {"since": "1"},
                "post": False,
                "listing_count": 80,
                "active_count": 40,
            },
            {
                "ok": True,
                "label": "GET listing/search {}",
                "controller": "listing",
                "method": "search",
                "params": {},
                "post": False,
                "listing_count": 4200,
                "active_count": 4100,
                "sample": [{"list_dt": "2026-01-01 00:00:00"}],
            },
        ]
    )
    assert chosen["kind"] == "api"
    assert chosen["method"] == "search"


def test_choose_enumerator_falls_back_to_old_newtoday():
    chosen = choose_enumerator(
        [
            {
                "ok": True,
                "label": "GET listing/newtoday default",
                "controller": "listing",
                "method": "newtoday",
                "params": {"since": "1"},
                "post": False,
                "listing_count": 40,
                "active_count": 20,
            },
            {
                "ok": True,
                "label": "GET listing/newtoday since=2016-01-01",
                "controller": "listing",
                "method": "newtoday",
                "params": {"since": "1451606400"},
                "post": False,
                "listing_count": 900,
                "active_count": 400,
            },
            {
                "ok": False,
                "label": "GET listing/search {}",
                "controller": "listing",
                "method": "search",
                "params": {},
                "post": False,
                "listing_count": 0,
                "active_count": 0,
            },
        ]
    )
    assert chosen["kind"] == "newtoday"
    assert chosen["since"] == "1451606400"


def test_has_atlantic_today_event_ignores_scrape_timestamps():
    today = datetime(2026, 8, 24, 12, 0, 0)
    document = {
        "date_updated": today,
        "listed_on": datetime(2020, 1, 1),
        "price_changed_on": None,
        "pending_on": None,
        "sold_on": None,
    }
    assert has_atlantic_today_event(document, now=today) is False
    document["listed_on"] = today
    assert has_atlantic_today_event(document, now=today) is True
