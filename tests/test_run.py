"""Phase 7 gate: the run lock, the telemetry row, and the API cache refresh."""

from dataclasses import replace
from datetime import timedelta

import pytest
import requests

from scraper import run as run_module
from scraper.normalize import utcnow
from scraper.run import LOCK_NAME, RunLocked, log_summary, refresh_api_index, scrape
from scraper.store import RunSummary


class StubClient:
    """A transport that returns an empty activity feed."""

    base_url = "https://www.viewpoint.ca"

    def __init__(self, activity=None):
        self.activity = activity or []
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc_info):
        return False

    def new_today_activity(self, since=None):
        return list(self.activity)

    def fetch_listing(self, url):
        listing_id = url.rstrip("/").split("/")[-2]
        return {
            "url": url,
            "Address": "1 Test Road, Halifax",
            "Price": "$400,000",
            "Status": "FOR SALE",
            "PID": f"pid-{listing_id}",
            "latitude": "44.65",
            "longitude": "-63.57",
            "Photos": [],
        }


@pytest.fixture
def stub_client(monkeypatch):
    client = StubClient()
    monkeypatch.setattr(run_module, "open_client", lambda settings=None: client)
    return client


class OkResponse:
    status_code = 200

    def raise_for_status(self):
        return None


@pytest.fixture(autouse=True)
def no_api_calls(monkeypatch):
    """Nothing in these tests may reach the network."""
    calls = []

    def record(url, **kwargs):
        calls.append(url)
        return OkResponse()

    monkeypatch.setattr(run_module.requests, "post", record)
    return calls


@pytest.fixture
def api_settings(settings, monkeypatch):
    """Settings that point at an Acres-API, without touching the real ones."""
    configured = replace(settings, acres_api_url="https://api.example.com")
    monkeypatch.setattr(run_module, "get_settings", lambda: configured)
    return configured


# -- the lock ------------------------------------------------------------


def test_a_second_run_exits_rather_than_overlapping(store, stub_client):
    assert store.acquire_lock(LOCK_NAME, 3600), "the first run should take the lock"

    with pytest.raises(RunLocked):
        scrape(store=store)

    assert stub_client.entered == 0, "the locked run still opened a browser session"


def test_the_lock_is_released_when_the_run_finishes(store, stub_client):
    scrape(store=store)

    assert store.locks.count_documents({"_id": LOCK_NAME}) == 0
    scrape(store=store)


def test_the_lock_is_released_when_the_run_crashes(store, monkeypatch):
    class ExplodingClient(StubClient):
        def new_today_activity(self, since=None):
            raise RuntimeError("viewpoint is down")

    monkeypatch.setattr(run_module, "open_client", lambda settings=None: ExplodingClient())

    with pytest.raises(RuntimeError):
        scrape(store=store)

    assert store.locks.count_documents({"_id": LOCK_NAME}) == 0


def test_an_abandoned_lock_expires_instead_of_blocking_forever(store, stub_client):
    store.locks.insert_one(
        {
            "_id": LOCK_NAME,
            "acquired_at": utcnow() - timedelta(hours=4),
            "expires_at": utcnow() - timedelta(hours=3),
        }
    )

    scrape(store=store)

    assert store.scrape_runs.count_documents({}) == 1


def test_a_manual_run_can_skip_the_lock(store, stub_client):
    store.acquire_lock(LOCK_NAME, 3600)

    scrape(store=store, use_lock=False)

    assert store.scrape_runs.count_documents({}) == 1
    assert store.locks.count_documents({"_id": LOCK_NAME}) == 1, "the held lock was stolen"


# -- telemetry -----------------------------------------------------------


def test_every_run_leaves_a_row_behind(store, stub_client):
    stub_client.activity = [
        {
            "listing_id": "202600001",
            "class_id": "1",
            "status_id": "5",
            "list_price": "400000",
            "url": "https://www.viewpoint.ca/cutsheet/202600001/1",
        }
    ]

    scrape(store=store)

    record = store.scrape_runs.find_one({})
    assert record["new_listings"] == 1
    assert record["listings_seen"] == 1
    assert record["errors"] == []
    assert record["duration_seconds"] >= 0
    assert record["finished_at"] >= record["started_at"]


def test_a_failed_run_is_recorded_with_its_error(store, monkeypatch):
    class ExplodingClient(StubClient):
        def new_today_activity(self, since=None):
            raise RuntimeError("viewpoint is down")

    monkeypatch.setattr(run_module, "open_client", lambda settings=None: ExplodingClient())

    with pytest.raises(RuntimeError):
        scrape(store=store)

    record = store.scrape_runs.find_one({})
    assert record is not None, "a crashed run left no trace"
    assert "viewpoint is down" in record["errors"][0]


def test_the_summary_logs_as_one_line(store, caplog):
    summary = RunSummary()
    summary.new_listings = 2
    summary.sold = 1
    summary.finished_at = utcnow()

    with caplog.at_level("INFO"):
        log_summary(summary)

    line = caplog.records[-1].getMessage()
    assert "new=2" in line and "sold=1" in line and "errors=0" in line


# -- the API cache -------------------------------------------------------


def test_a_run_that_changed_nothing_leaves_the_api_alone(store, stub_client, no_api_calls):
    scrape(store=store)

    assert no_api_calls == [], "the API index was rebuilt for no reason"


def test_a_run_that_changed_something_refreshes_the_api(
    store, stub_client, no_api_calls, api_settings
):
    stub_client.activity = [
        {
            "listing_id": "202600001",
            "class_id": "1",
            "status_id": "5",
            "list_price": "400000",
            "url": "https://www.viewpoint.ca/cutsheet/202600001/1",
        }
    ]

    scrape(store=store)

    assert no_api_calls == ["https://api.example.com/api/index/refresh"]


def test_a_missing_api_url_is_not_an_error(settings, no_api_calls):
    assert refresh_api_index(replace(settings, acres_api_url="")) is False
    assert no_api_calls == []


def test_an_unreachable_api_does_not_fail_the_run(api_settings, monkeypatch):
    def explode(url, **kwargs):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(run_module.requests, "post", explode)

    assert refresh_api_index(api_settings) is False
