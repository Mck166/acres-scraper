"""Phase 1 gate: the HTTP transport works against the real site.

Marked live because it signs in to viewpoint.ca.

    pytest -m live tests/test_live_api.py
"""

import pytest

from scraper.client import ViewpointClient
from scraper.config import get_settings
from scraper.identity import listing_id_from_url
from scraper.normalize import normalize_document
from scraper.photos import parse_photo_url

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def client():
    settings = get_settings()
    if not settings.viewpoint_user or not settings.viewpoint_pass:
        pytest.skip("VIEWPOINT_USER / VIEWPOINT_PASS not configured")
    with ViewpointClient(settings) as client:
        yield client


@pytest.fixture(scope="module")
def activity(client):
    entries = client.new_today_activity()
    if not entries:
        pytest.skip("new-today returned nothing today")
    return entries


def test_login_bootstraps_the_api_parameters(client):
    assert client.client_ver
    assert client.api_key_id and client.api_key_hash


def test_new_today_returns_the_days_activity(activity):
    assert len(activity) > 0
    assert all(entry.get("listing_id") for entry in activity)
    assert all(listing_id_from_url(entry["url"]) for entry in activity)

    # The feed is an activity list, so it must carry more than brand new listings.
    statuses = {entry.get("status_id") for entry in activity}
    assert len(statuses) > 1, f"expected a mix of listing states, saw {statuses}"


def test_activity_carries_coordinates_so_geocoding_is_not_needed(activity):
    located = [
        entry for entry in activity if entry.get("latitude") and entry.get("longitude")
    ]
    assert len(located) > len(activity) * 0.8, (
        f"only {len(located)} of {len(activity)} listings had coordinates"
    )


def test_photos_endpoint_returns_a_full_set(client, activity):
    with_photos = next(
        entry for entry in activity if int(entry.get("pix_count") or 0) >= 5
    )
    body = client.listing_photos(with_photos["listing_id"], with_photos["class_id"])

    photos = body["photos"]
    assert len(photos) >= 5
    assert len(photos) == int(with_photos["pix_count"])


def test_fetch_listing_matches_what_the_browser_transport_sees(client, activity):
    """The HTTP path must not lose fields the Selenium path used to collect."""
    url = activity[0]["url"]
    raw = client.fetch_listing(url)
    document = normalize_document(raw, url=url)

    assert document["listing_id"]
    assert document["Address"]
    assert document["Status"] in ("FOR SALE", "PENDING SALE", "SOLD", "EXPIRED")
    assert document["latitude"] is not None
    assert document["longitude"] is not None

    photos = document["Photos"]
    assert document["Photo_Count"] == len(photos)
    assert all(parse_photo_url(photo) for photo in photos)


def test_api_transport_sees_everything_the_browser_does(client, activity):
    """Cross-check the two transports so the switch cannot silently lose listings."""
    from scraper.selenium_client import SeleniumClient

    with SeleniumClient(get_settings()) as browser:
        browser_ids = {listing_id_from_url(url) for url in browser.new_today_urls()}

    api_ids = {entry["listing_id"] for entry in activity}
    missed = browser_ids - api_ids

    assert not missed, f"the API transport missed listings the browser found: {sorted(missed)}"
    assert len(api_ids) >= len(browser_ids)


def test_a_detailed_listing_carries_the_mls_block(client, activity):
    """Residential listings must still fill the app's detail screen."""
    residential = next(
        (entry for entry in activity if entry.get("class_id") == "1" and entry.get("mla")),
        None,
    )
    if residential is None:
        pytest.skip("no residential listing with living area in today's activity")

    raw = client.fetch_listing(residential["url"])

    for field in ("PID", "TYPE", "BEDS", "BATHROOMS (F/H)", "ROOF", "LISTED BY"):
        assert field in raw, f"{field} missing from {residential['url']}"
