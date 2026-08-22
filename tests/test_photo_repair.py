"""Phase 2 gate: repair the photo sets that the old extraction broke.

The production database holds documents with zero or one photo because the old
gallery click-through stopped after the first slide. This copies those exact
documents into the test database, rebuilds their photo sets against the live
site, and checks the result is actually usable.

Marked live: it reads production, writes only to the test database, and fetches
from viewpoint.

    pytest -m live tests/test_photo_repair.py
"""

import hashlib

import pytest
import requests

from scraper.backfill import copy_documents, find_broken_photo_sets, repair_photos
from scraper.client import ViewpointClient
from scraper.config import get_settings
from scraper.photos import PROBE_INDEX, build_photo_url, parse_photo_url

pytestmark = pytest.mark.live

SAMPLE_SIZE = 25


@pytest.fixture(scope="module")
def client():
    settings = get_settings()
    if not settings.viewpoint_user or not settings.viewpoint_pass:
        pytest.skip("VIEWPOINT_USER / VIEWPOINT_PASS not configured")
    with ViewpointClient(settings) as client:
        yield client


@pytest.fixture
def seeded_store(store, mongo_client, settings):
    """The real broken documents, copied into the test database."""
    production = mongo_client[settings.mongodb_db_name][settings.properties_collection]
    broken = find_broken_photo_sets(production, limit=SAMPLE_SIZE)
    if not broken:
        pytest.skip("production has no broken photo sets left to repair")

    full = production.find({"_id": {"$in": [doc["_id"] for doc in broken]}})
    copied = copy_documents(production, store.properties, full)
    assert copied == len(broken)
    return store


def test_production_still_has_broken_photo_sets(mongo_client, settings):
    """Establishes the problem this phase exists to fix."""
    production = mongo_client[settings.mongodb_db_name][settings.properties_collection]
    broken = find_broken_photo_sets(production)
    assert broken, "nothing to repair"
    print(f"\n{len(broken)} documents have a suspect photo set")


def test_repair_rebuilds_the_photo_sets(client, seeded_store):
    before = find_broken_photo_sets(seeded_store.properties)
    summary = repair_photos(client, seeded_store.properties)

    assert summary["examined"] == len(before)
    assert summary["repaired"] > 0, "no document gained photos"
    assert summary["failed"] == 0, "some documents could not be rebuilt"

    repaired = list(seeded_store.properties.find({}))

    for doc in repaired:
        url = doc.get("url")
        photos = doc.get("Photos") or []

        assert doc.get("Photo_Count") == len(photos), f"{url}: count disagrees with the list"
        assert len(set(photos)) == len(photos), f"{url}: duplicate photo URLs"
        assert all(parse_photo_url(photo) for photo in photos), f"{url}: malformed photo URL"

        indices = [int(parse_photo_url(photo)["index"]) for photo in photos]
        assert indices == list(range(1, len(photos) + 1)), f"{url}: photo indices are not 1..N"

    still_thin = [doc for doc in repaired if len(doc.get("Photos") or []) <= 1]
    print(f"\nrepaired {summary['repaired']}, unchanged {summary['unchanged']}")
    print(f"{len(still_thin)} of {len(repaired)} documents still have 0 or 1 photo")


def test_repaired_urls_serve_real_images(client, seeded_store):
    """A URL is only good if it returns a photo rather than the stand-in image.

    Viewpoint answers out-of-range indices with HTTP 200 and a placeholder, so
    checking the status code proves nothing. The placeholder is fetched first
    and compared against.
    """
    repair_photos(client, seeded_store.properties)

    checked = 0
    for doc in seeded_store.properties.find({"Photos.1": {"$exists": True}}).limit(5):
        photos = doc["Photos"]
        parsed = parse_photo_url(photos[0])
        listing_id, class_id, cch = parsed["listing_id"], parsed["class_id"], parsed["cch"]

        placeholder = requests.get(
            build_photo_url(listing_id, class_id, PROBE_INDEX, cch), timeout=30
        )
        placeholder_digest = hashlib.md5(placeholder.content).hexdigest()

        for photo in (photos[0], photos[-1]):
            response = requests.get(photo, timeout=30)
            assert response.status_code == 200, f"{photo} returned {response.status_code}"
            digest = hashlib.md5(response.content).hexdigest()
            assert digest != placeholder_digest, f"{photo} is the placeholder, not a real photo"
            checked += 1

    assert checked > 0, "no repaired document had photos to check"
    print(f"\nverified {checked} photo URLs serve real images")


def test_repair_is_idempotent(client, seeded_store):
    first = repair_photos(client, seeded_store.properties)
    second = repair_photos(client, seeded_store.properties)

    assert second["repaired"] == 0, "a second pass changed documents again"
    assert second["examined"] <= first["examined"]
