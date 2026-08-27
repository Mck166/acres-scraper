"""The HTTP transport, exercised offline against recorded responses."""

import json
import pathlib

import pytest

from scraper.client import ViewpointClient, ViewpointError
from scraper.config import Settings

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CUTSHEET_HTML = FIXTURES / "pages" / "cutsheet_202603269_49photos.html"

BOOTSTRAP_PAGE = """
<html><body><script>
    var vp = {
        CLIENT_VER:'23502',
        NONCES:["7e90e3f1b40817be54c6f4530db7d772","3aed9485bd96f5b319714ef664ed78c3"],
        APIKEY:{
            id:1,
            hash:'d46db3ef736c3c38700ef7f3fe0170dde1d2cbc25e6a296481b1ef75a1459b53'
        }
    };
</script></body></html>
"""


def make_settings(**overrides) -> Settings:
    defaults = dict(
        viewpoint_user="tester@example.com",
        viewpoint_pass="secret",
        viewpoint_base_url="https://www.viewpoint.ca",
        mongodb_uri="mongodb://localhost:27017/",
        mongodb_db_name="test",
        properties_collection="properties",
        sold_collection="sold_properties",
        recent_updates_collection="recent_updates",
        geocode_cache_collection="geocode_cache",
        scrape_runs_collection="scrape_runs",
        locks_collection="scraper_locks",
        notification_events_collection="notification_events",
        transport="api",
        stale_recheck_limit=25,
        recent_updates_ttl_seconds=86400,
        request_delay_seconds=0.0,
        run_lock_ttl_seconds=3600,
        acres_api_url="",
        scraper_api_secret="",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeResponse:
    def __init__(self, body=None, text="", status_code=200):
        self._body = body
        self.text = text
        self.status_code = status_code

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakeSession:
    """Stands in for requests.Session, replaying queued responses."""

    def __init__(self):
        self.headers = {}
        self.get_responses = []
        self.post_responses = []
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("GET", url, params))
        return self.get_responses.pop(0)

    def post(self, url, data=None, timeout=None, headers=None):
        self.calls.append(("POST", url, data))
        return self.post_responses.pop(0)

    def close(self):
        pass


@pytest.fixture
def client():
    session = FakeSession()
    return ViewpointClient(make_settings(), session=session)


def test_bootstrap_reads_the_pages_api_parameters(client):
    client.session.get_responses.append(FakeResponse(text=BOOTSTRAP_PAGE))

    client.bootstrap()

    assert client.client_ver == "23502"
    assert client.api_key_id == "1"
    assert client.api_key_hash.startswith("d46db3ef")
    assert len(client._nonces) == 2


def test_bootstrap_fails_loudly_without_client_version(client):
    client.session.get_responses.append(FakeResponse(text="<html></html>"))

    with pytest.raises(ViewpointError, match="CLIENT_VER"):
        client.bootstrap()


def test_each_call_spends_a_nonce_and_banks_the_next(client):
    client.client_ver = "23502"
    client._nonces = ["first"]
    client.session.get_responses.append(
        FakeResponse(body={"status": "success", "nonce": "second", "photos": []})
    )

    client.call("listing", "photos", {"listing_id": "1", "class_id": "1"})

    _, _, params = client.session.calls[0]
    assert params["nonce"] == "first"
    assert params["CLIENT_VER"] == "23502"
    assert client._nonces == ["second"]


def test_call_retries_then_reports_the_error(client):
    """A rejected call drops its nonce pool and mints a fresh one before retrying."""
    client.client_ver = "23502"
    client.api_key_id, client.api_key_hash = "1", "hash"
    client._nonces = ["first"]

    for _ in range(3):
        client.session.get_responses.append(
            FakeResponse(body={"status": "error", "errors": [{"message": "Invalid listing"}]})
        )
    for _ in range(2):
        client.session.post_responses.append(FakeResponse(body={"status": "success", "nonce": "minted"}))

    with pytest.raises(ViewpointError, match="Invalid listing"):
        client.call("listing", "details", {"listing_id": "1"})

    attempts = [call for call in client.session.calls if call[0] == "GET"]
    mints = [call for call in client.session.calls if call[1].endswith("/api/nonce")]
    assert len(attempts) == 3
    assert len(mints) == 2


def test_new_today_activity_pairs_listings_with_their_events(client, monkeypatch):
    body = json.loads((FIXTURES / "viewpoint" / "newtoday.json").read_text())
    monkeypatch.setattr(client, "new_today", lambda since="": body)

    activity = client.new_today_activity()

    assert len(activity) == len(body["listings"])
    assert all(entry["url"].startswith("https://www.viewpoint.ca/cutsheet/") for entry in activity)

    # Cutsheet URLs address a listing by id and property class.
    sample = activity[0]
    assert sample["url"].endswith(f"/cutsheet/{sample['listing_id']}/{sample['class_id']}")

    with_events = [entry for entry in activity if entry["events"]]
    assert with_events, "no listing was matched to a change event"


def test_new_today_urls_are_unique_and_well_formed(client, monkeypatch):
    body = json.loads((FIXTURES / "viewpoint" / "newtoday.json").read_text())
    monkeypatch.setattr(client, "new_today", lambda since="": body)

    urls = client.new_today_urls()
    assert len(urls) == len(set(urls))


def test_fetch_listing_builds_a_complete_document(client, monkeypatch):
    monkeypatch.setattr(client, "fetch_page", lambda url: CUTSHEET_HTML.read_text())

    raw = client.fetch_listing("https://www.viewpoint.ca/cutsheet/202603269/1")

    assert raw["Address"] == "523 Chebucto Street, Baddeck"
    assert raw["Status"] == "SOLD"
    assert raw["Price"] == "$499,000"
    assert raw["PID"] == "85019180"
    assert raw["BEDS"] == "4"
    assert raw["BASEMENT"] == "Full, Partial, Crawl Space, Other"
    assert raw["latitude"] == "46.10131396"
    assert raw["longitude"] == "-60.74911817"
    assert raw["Description"].startswith("Own a true piece of Baddeck history")
    assert raw["listed_on"] == "2026-02-23 00:00:00"
    assert raw["sold_on"] == "2026-08-22 00:00:00"


def test_fetch_listing_recovers_every_photo(client, monkeypatch):
    """The page renders four photos; the listing has 49."""
    monkeypatch.setattr(client, "fetch_page", lambda url: CUTSHEET_HTML.read_text())

    raw = client.fetch_listing("https://www.viewpoint.ca/cutsheet/202603269/1")

    assert raw["Photo_Count"] == 49
    assert len(raw["Photos"]) == 49
    assert len(set(raw["Photos"])) == 49


def test_fetch_listing_rejects_non_cutsheet_urls(client):
    assert client.fetch_listing("https://www.viewpoint.ca/map") is None


def test_recorded_photos_response_has_a_usable_set():
    body = json.loads((FIXTURES / "viewpoint" / "photos.json").read_text())
    photos = body["photos"]

    assert len(photos) >= 5
    assert all(photo["src"].endswith(".jpg") for photo in photos)
