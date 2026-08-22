"""Phase 0 gate: the Selenium transport still scrapes a real listing end to end.

Marked live because it logs in to viewpoint.ca and drives a real browser.

    pytest -m live tests/test_live_selenium.py
"""

import pytest

from scraper.config import get_settings
from scraper.identity import listing_id_from_url
from scraper.normalize import normalize_document
from scraper.photos import parse_photo_url

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def selenium_client():
    settings = get_settings()
    if not settings.viewpoint_user or not settings.viewpoint_pass:
        pytest.skip("VIEWPOINT_USER / VIEWPOINT_PASS not configured")

    from scraper.selenium_client import SeleniumClient

    client = SeleniumClient(settings)
    with client:
        yield client


def test_new_today_list_returns_cutsheet_urls(selenium_client):
    urls = selenium_client.new_today_urls()
    assert urls, "new-today list produced no cutsheet links"
    assert all(listing_id_from_url(url) for url in urls), "some URLs had no listing id"


def test_single_listing_scrapes_end_to_end(selenium_client):
    urls = selenium_client.new_today_urls()
    raw = selenium_client.fetch_listing(urls[0])

    assert raw, "scraping the first listing returned nothing"

    document = normalize_document(raw, url=urls[0])

    assert document["listing_id"], "listing id missing"
    assert document["Address"], "address missing"
    assert document["Price"], "price missing"
    assert document["Status"] in ("FOR SALE", "PENDING SALE", "SOLD")

    photos = document["Photos"]
    assert photos, "no photos extracted"
    assert document["Photo_Count"] == len(photos)
    assert all(parse_photo_url(url) for url in photos), "photo URLs are not viewpoint photo URLs"
