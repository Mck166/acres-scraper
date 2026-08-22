"""Phase 4 gate: classification, idempotency, and the stale re-check.

Classification runs entirely on the activity feed's own fields, so it can be
tested with synthetic entries and no network at all.
"""

from datetime import timedelta

import pytest

from scraper.normalize import STATUS_EXPIRED, STATUS_FOR_SALE, STATUS_SOLD, utcnow
from scraper.store import (
    CHANGE_DELISTED,
    CHANGE_NEW,
    CHANGE_NONE,
    CHANGE_PRICE,
    CHANGE_SOLD,
    RunSummary,
)
from scraper.sync import Change, apply_change, classify, plan_changes, stale_recheck, sync

ACTIVE, SOLD_PENDING, SOLD_CLOSED, EXPIRED = "5", "6", "2", "1"


def entry(listing_id="202600001", status_id=ACTIVE, list_price="400000", sold_price=None, **extra):
    payload = {
        "listing_id": listing_id,
        "class_id": "1",
        "status_id": status_id,
        "list_price": list_price,
        "sold_price": sold_price,
        "url": f"https://www.viewpoint.ca/cutsheet/{listing_id}/1",
    }
    payload.update(extra)
    return payload


def stored(listing_id="202600001", price="$400,000", status=STATUS_FOR_SALE, **extra):
    doc = {
        "_id": f"oid-{listing_id}",
        "listing_id": listing_id,
        "url": f"https://www.viewpoint.ca/cutsheet/{listing_id}/1",
        "Price": price,
        "Status": status,
    }
    doc.update(extra)
    return doc


# -- classification ------------------------------------------------------


def test_a_listing_we_have_never_seen_is_new():
    change = classify(entry(), active=None, archived=None)
    assert change.kind == CHANGE_NEW
    assert change.needs_detail


def test_an_unchanged_listing_is_left_alone():
    change = classify(entry(list_price="400000"), active=stored(price="$400,000"), archived=None)
    assert change.kind == CHANGE_NONE
    assert not change.needs_detail


def test_a_price_cut_is_a_price_change():
    change = classify(entry(list_price="380000"), active=stored(price="$400,000"), archived=None)
    assert change.kind == CHANGE_PRICE
    assert change.old_price == 400000
    assert change.new_price == 380000
    assert change.needs_detail


def test_a_price_rise_is_a_price_change():
    change = classify(entry(list_price="425000"), active=stored(price="$400,000"), archived=None)
    assert change.kind == CHANGE_PRICE
    assert change.new_price == 425000


@pytest.mark.parametrize("status_id", [SOLD_PENDING, SOLD_CLOSED])
def test_both_sold_states_count_as_sold(status_id):
    change = classify(
        entry(status_id=status_id, sold_price="390000"), active=stored(), archived=None
    )
    assert change.kind == CHANGE_SOLD
    assert change.sold_price == 390000
    assert not change.needs_detail, "a listing we hold can be retired without a fetch"


def test_a_listing_taken_off_the_market_is_delisted():
    change = classify(entry(status_id=EXPIRED), active=stored(), archived=None)
    assert change.kind == CHANGE_DELISTED
    assert not change.needs_detail


def test_a_sale_of_a_listing_we_never_held_is_still_recorded():
    """This is how the sold archive fills out for homes we never saw listed."""
    change = classify(entry(status_id=SOLD_CLOSED, sold_price="390000"), active=None, archived=None)
    assert change.kind == CHANGE_SOLD
    assert change.needs_detail, "we have nothing stored, so it must be fetched"


def test_an_expiry_of_a_listing_we_never_held_is_ignored():
    change = classify(entry(status_id=EXPIRED), active=None, archived=None)
    assert change.kind == CHANGE_NONE


def test_an_already_archived_listing_is_not_archived_again():
    change = classify(entry(status_id=SOLD_CLOSED), active=None, archived=stored(status=STATUS_SOLD))
    assert change.kind == CHANGE_NONE


def test_an_archived_listing_back_on_the_market_is_new_again():
    change = classify(entry(status_id=ACTIVE), active=None, archived=stored(status=STATUS_SOLD))
    assert change.kind == CHANGE_NEW


def test_a_missing_price_is_not_treated_as_a_change():
    change = classify(entry(list_price=None), active=stored(price="$400,000"), archived=None)
    assert change.kind == CHANGE_NONE


def test_classification_reads_the_status_id_not_the_text():
    change = classify(entry(status_id=SOLD_CLOSED, status="whatever"), active=stored(), archived=None)
    assert change.status == STATUS_SOLD


# -- planning against the database ---------------------------------------


def test_plan_changes_matches_entries_to_stored_listings(store):
    store.properties.insert_one(stored("202600001", price="$400,000"))
    store.sold.insert_one(stored("202600002", status=STATUS_SOLD))

    changes = {
        change.listing_id: change
        for change in plan_changes(
            [
                entry("202600001", list_price="380000"),
                entry("202600002", status_id=SOLD_CLOSED),
                entry("202600003"),
            ],
            store,
        )
    }

    assert changes["202600001"].kind == CHANGE_PRICE
    assert changes["202600002"].kind == CHANGE_NONE
    assert changes["202600003"].kind == CHANGE_NEW


def test_plan_changes_ignores_entries_without_a_listing_id(store):
    assert plan_changes([{"class_id": "1"}], store) == []


# -- applying changes ----------------------------------------------------


class RecordingClient:
    """A client that serves canned listing pages and counts fetches.

    A listing's page has to agree with the activity feed about its price, the
    same way the real site does, or the engine would be tested against a
    contradiction that cannot happen.
    """

    base_url = "https://www.viewpoint.ca"
    DEFAULT_PRICE = "400000"

    def __init__(self, prices=None, statuses=None):
        self.prices = dict(prices or {})
        self.statuses = dict(statuses or {})
        self.fetches = []

    def price_of(self, listing_id):
        return self.prices.get(listing_id, self.DEFAULT_PRICE)

    def fetch_listing(self, url):
        self.fetches.append(url)
        listing_id = url.rstrip("/").split("/")[-2]
        return {
            "url": url,
            "Address": f"{listing_id} Test Street, Halifax",
            "Price": f"${int(self.price_of(listing_id)):,}",
            "Status": self.statuses.get(listing_id, "FOR SALE"),
            "PID": f"pid-{listing_id}",
            "BEDS": "3",
            "latitude": "44.65",
            "longitude": "-63.57",
            "Photos": [
                f"https://www.viewpoint.ca/property/cutimagel/{listing_id}/1/1.jpg?&sd=summary&cch=aa"
            ],
        }

    def new_today_activity(self, since=None):
        return []


def test_retiring_a_listing_we_hold_costs_no_fetch(store):
    store.properties.insert_one(stored("202600001"))
    client = RecordingClient()
    summary = RunSummary()

    change = classify(
        entry("202600001", status_id=SOLD_CLOSED, sold_price="390000"),
        active=store.properties.find_one({"listing_id": "202600001"}),
        archived=None,
    )
    apply_change(client, store, change, summary)

    assert client.fetches == [], "archiving should not have fetched anything"
    assert summary.sold == 1
    assert store.properties.count_documents({}) == 0
    assert store.sold.count_documents({}) == 1


def test_unchanged_listings_are_skipped_without_a_fetch(store):
    store.properties.insert_one(stored("202600001", price="$400,000"))
    client = RecordingClient()
    summary = RunSummary()

    change = classify(
        entry("202600001", list_price="400000"),
        active=store.properties.find_one({"listing_id": "202600001"}),
        archived=None,
    )
    apply_change(client, store, change, summary)

    assert client.fetches == []
    assert summary.skipped == 1


# -- idempotency ---------------------------------------------------------


def test_a_second_run_over_the_same_feed_writes_nothing(store):
    client = RecordingClient(prices={"202600002": "250000"})
    activity = [entry("202600001"), entry("202600002", list_price="250000")]

    first = sync(client, store, activity=activity, stale_limit=0)
    assert first.new_listings == 2
    assert first.writes == 2

    fetches_after_first = len(client.fetches)
    before = list(store.properties.find({}).sort("listing_id", 1))

    second = sync(client, store, activity=activity, stale_limit=0)

    assert second.writes == 0, "the second run changed something"
    assert second.skipped == 2
    assert len(client.fetches) == fetches_after_first, "the second run fetched pages again"

    after = list(store.properties.find({}).sort("listing_id", 1))
    assert before == after, "documents were rewritten by an unchanged run"


def test_a_run_only_fetches_listings_that_actually_changed(store):
    client = RecordingClient()
    sync(client, store, activity=[entry("202600001"), entry("202600002")], stale_limit=0)
    client.fetches.clear()

    # 202600002 is repriced on both the feed and its page, as it would be live.
    client.prices["202600002"] = "333000"
    summary = sync(
        client,
        store,
        activity=[entry("202600001"), entry("202600002", list_price="333000")],
        stale_limit=0,
    )

    assert len(client.fetches) == 1, "only the repriced listing should have been fetched"
    assert summary.price_changes == 1
    assert summary.skipped == 1


def test_recent_updates_records_only_notifiable_changes(store):
    client = RecordingClient()
    sync(client, store, activity=[entry("202600001")], stale_limit=0)

    store.properties.insert_one(stored("202600009"))
    sync(client, store, activity=[entry("202600009", status_id=EXPIRED)], stale_limit=0)

    kinds = {doc["change_type"] for doc in store.recent_updates.find({})}
    assert CHANGE_NEW in kinds
    assert CHANGE_DELISTED not in kinds, "a delisting is not something to swipe on"


# -- stale re-check ------------------------------------------------------


def test_stale_recheck_picks_the_least_recently_seen(store):
    now = utcnow()
    for index in range(5):
        store.properties.insert_one(
            stored(f"20260000{index}", date_updated=now - timedelta(days=index))
        )

    selected = [doc["listing_id"] for doc in store.stale_active_listings(2)]

    assert selected == ["202600004", "202600003"], "expected the two oldest"


def test_stale_recheck_respects_its_limit(store):
    now = utcnow()
    for index in range(5):
        store.properties.insert_one(
            stored(f"20260000{index}", date_updated=now - timedelta(days=index))
        )

    assert len(store.stale_active_listings(3)) == 3
    assert store.stale_active_listings(0) == []


def test_stale_recheck_never_selects_archived_listings(store):
    now = utcnow()
    store.properties.insert_one(stored("202600001", date_updated=now))
    store.sold.insert_one(stored("202600002", status=STATUS_SOLD, date_updated=now - timedelta(days=99)))

    selected = [doc["listing_id"] for doc in store.stale_active_listings(10)]

    assert selected == ["202600001"]


def test_stale_recheck_can_be_switched_off(store):
    store.properties.insert_one(stored("202600001"))
    client = RecordingClient()
    summary = RunSummary()

    stale_recheck(client, store, summary, limit=0)

    assert summary.stale_rechecked == 0
    assert client.fetches == []


def test_stale_recheck_leaves_alone_what_the_feed_just_covered(store):
    client = RecordingClient()

    summary = sync(client, store, activity=[entry("202600001")], stale_limit=25)

    assert len(client.fetches) == 1, "the listing was fetched twice in one run"
    assert summary.stale_rechecked == 0
    assert summary.listings_seen == 1


def test_stale_recheck_refetches_and_updates(store):
    store.properties.insert_one(stored("202600001", price="$999,000"))
    client = RecordingClient()
    summary = RunSummary()

    stale_recheck(client, store, summary, limit=5)

    assert summary.stale_rechecked == 1
    assert len(client.fetches) == 1
    assert store.properties.find_one({"listing_id": "202600001"})["Price"] == "$400,000"
