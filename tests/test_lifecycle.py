"""Phase 5 gate: a listing's whole life, from first appearance to relisting.

The archive is the part of the system with the most ways to go quietly wrong.
A sold listing keeps its ``_id`` because ``favorites`` and ``dislikes`` in
Acres-API store ``str(_id)``; losing it orphans every saved home. A listing must
also never exist in both collections at once, or the map would show it twice.

These tests drive the real sync engine against a real MongoDB so the unique and
TTL indexes are exercised rather than mocked.
"""

from datetime import timedelta

import pytest

from scraper.normalize import STATUS_FOR_SALE, STATUS_SOLD, utcnow
from scraper.store import CHANGE_NEW, CHANGE_PRICE, CHANGE_SOLD
from scraper.sync import sync

ACTIVE, SOLD_CLOSED, EXPIRED = "5", "2", "1"

SHARED_PID = "40123456"


def entry(listing_id, status_id=ACTIVE, list_price="400000", sold_price=None):
    return {
        "listing_id": listing_id,
        "class_id": "1",
        "status_id": status_id,
        "list_price": list_price,
        "sold_price": sold_price,
        "url": f"https://www.viewpoint.ca/cutsheet/{listing_id}/1",
    }


class LifecycleClient:
    """Serves listing pages that agree with whatever the feed last reported."""

    base_url = "https://www.viewpoint.ca"

    def __init__(self):
        self.prices = {}
        self.statuses = {}
        self.pids = {}
        self.sold_prices = {}
        self.fetches = []

    def fetch_listing(self, url):
        self.fetches.append(url)
        listing_id = url.rstrip("/").split("/")[-2]
        document = {
            "url": url,
            "Address": "12 Bluenose Lane, Chester",
            "Price": f"${int(self.prices.get(listing_id, 400000)):,}",
            "Status": self.statuses.get(listing_id, STATUS_FOR_SALE),
            "PID": self.pids.get(listing_id, SHARED_PID),
            "BEDS": "3",
            "TYPE": "Single Family",
            "latitude": "44.54",
            "longitude": "-64.24",
            "Photos": [
                f"{self.base_url}/property/cutimagel/{listing_id}/1/{n}.jpg?&sd=summary&cch=aa"
                for n in range(1, 4)
            ],
        }
        # A sold cutsheet keeps showing the asking price and adds what it fetched.
        if listing_id in self.sold_prices:
            document["sold_price"] = self.sold_prices[listing_id]
        return document

    def new_today_activity(self, since=None):
        return []


@pytest.fixture
def client():
    return LifecycleClient()


def run(client, store, *entries):
    return sync(client, store, activity=list(entries), stale_limit=0)


# -- indexes -------------------------------------------------------------


def test_every_index_the_plan_requires_exists(store, settings):
    for collection in (store.properties, store.sold):
        indexes = collection.index_information()
        keyed = {tuple(spec["key"])[0][0]: spec for spec in indexes.values()}

        assert keyed["listing_id"].get("unique") is True
        for field in ("PID", "Status", "date_added", "date_updated", "url"):
            assert field in keyed, f"{collection.name} is missing an index on {field}"


def test_recent_updates_expires_after_twenty_four_hours(store, settings):
    indexes = store.recent_updates.index_information()

    ttl = [spec for spec in indexes.values() if "expireAfterSeconds" in spec]
    assert ttl, "recent_updates has no TTL index"
    assert tuple(ttl[0]["key"])[0][0] == "changed_at"
    assert ttl[0]["expireAfterSeconds"] == settings.recent_updates_ttl_seconds == 86400


def test_a_listing_id_cannot_be_stored_twice(store):
    store.properties.insert_one({"listing_id": "202600001"})
    with pytest.raises(Exception):
        store.properties.insert_one({"listing_id": "202600001"})


# -- the lifecycle -------------------------------------------------------


def test_a_listing_lists_reprices_sells_and_relists(store, client):
    listed = run(client, store, entry("202600001", list_price="400000"))
    assert listed.new_listings == 1

    original = store.properties.find_one({"listing_id": "202600001"})
    original_id = original["_id"]
    assert original["Price"] == "$400,000"
    assert original["price_value"] == 400000
    assert original["PID"] == SHARED_PID

    # A user favourites it here. This is the id that must survive the sale.
    favourite = str(original_id)

    client.prices["202600001"] = 380000
    repriced = run(client, store, entry("202600001", list_price="380000"))
    assert repriced.price_changes == 1

    after_price_cut = store.properties.find_one({"_id": original_id})
    assert after_price_cut["Price"] == "$380,000"
    assert [item["price_value"] for item in after_price_cut["price_history"]] == [400000]
    assert after_price_cut["price_changed_at"] is not None

    sold = run(
        client, store, entry("202600001", status_id=SOLD_CLOSED, sold_price="375000")
    )
    assert sold.sold == 1
    assert client.fetches[-1].endswith("/202600001/1"), "the sale should not have refetched"

    assert store.properties.count_documents({"listing_id": "202600001"}) == 0
    archived = store.sold.find_one({"listing_id": "202600001"})
    assert str(archived["_id"]) == favourite, "the favourite no longer resolves"
    assert archived["Status"] == STATUS_SOLD
    assert archived["sold_price"] == 375000
    assert archived["sold_at"] is not None
    assert archived["archived_reason"] == "sold"
    assert archived["days_on_market"] >= 0

    # The same parcel comes back a year later under a fresh MLS number.
    client.pids["202700002"] = SHARED_PID
    client.prices["202700002"] = 450000
    relisted = run(client, store, entry("202700002", list_price="450000"))
    assert relisted.new_listings == 1
    assert relisted.relisted == 1

    fresh = store.properties.find_one({"listing_id": "202700002"})
    assert fresh["_id"] != original_id, "a relisting is a new listing, not the old one"
    assert fresh["relisted"] is True
    assert fresh["previous_listing_ids"] == ["202600001"]
    assert [sale["sold_price"] for sale in fresh["sale_history"]] == [375000]

    assert store.sold.find_one({"listing_id": "202600001"}) is not None, "history was lost"


def test_archiving_keeps_the_whole_listing_not_just_the_diff_fields(store, client):
    """The sale path never refetches, so the archive is built from what we hold.

    Classification only reads a projection of each active listing, and archiving
    rewrites the document, so anything outside that projection is exactly what a
    careless archive would drop.
    """
    run(client, store, entry("202600001"))
    listed = store.properties.find_one({"listing_id": "202600001"})

    run(client, store, entry("202600001", status_id=SOLD_CLOSED, sold_price="375000"))
    archived = store.sold.find_one({"listing_id": "202600001"})

    for field in ("Address", "Photos", "Photo_Count", "BEDS", "TYPE", "latitude", "longitude"):
        assert archived.get(field) == listed[field], f"{field} was lost when archiving"

    assert len(archived["Photos"]) == 3


def test_a_sale_we_never_saw_listed_is_archived_with_what_it_sold_for(store, client):
    """Most sales arrive this way: the first we hear of the home is its sale."""
    client.statuses["202600001"] = STATUS_SOLD
    client.prices["202600001"] = 400000
    client.sold_prices["202600001"] = 375000
    sold = sync(
        client,
        store,
        activity=[entry("202600001", status_id=SOLD_CLOSED, sold_price="375000")],
        stale_limit=0,
    )

    assert sold.sold == 1
    assert store.properties.count_documents({}) == 0

    archived = store.sold.find_one({"listing_id": "202600001"})
    assert archived["archived_reason"] == "sold"
    assert archived["archived_at"] is not None
    assert archived["Price"] == "$400,000", "the asking price is still worth keeping"
    assert archived["sold_price"] == 375000

    card = store.recent_updates.find_one({"change_type": CHANGE_SOLD})
    assert card["new_price"] == 375000, "a sold card should say what it sold for"


def test_no_listing_id_is_ever_in_both_collections(store, client):
    run(client, store, entry("202600001"), entry("202600002"))
    run(client, store, entry("202600001", status_id=SOLD_CLOSED, sold_price="375000"))

    client.pids["202700002"] = SHARED_PID
    run(client, store, entry("202700002"))

    active = {doc["listing_id"] for doc in store.properties.find({}, {"listing_id": 1})}
    archived = {doc["listing_id"] for doc in store.sold.find({}, {"listing_id": 1})}

    assert active & archived == set()
    assert active == {"202600002", "202700002"}
    assert archived == {"202600001"}


def test_recent_updates_holds_exactly_the_changes_worth_swiping(store, client):
    run(client, store, entry("202600001"))

    client.prices["202600001"] = 380000
    run(client, store, entry("202600001", list_price="380000"))

    run(client, store, entry("202600001", status_id=SOLD_CLOSED, sold_price="375000"))

    updates = {doc["change_type"]: doc for doc in store.recent_updates.find({})}
    assert set(updates) == {CHANGE_NEW, CHANGE_PRICE, CHANGE_SOLD}

    assert updates[CHANGE_PRICE]["old_price"] == 400000
    assert updates[CHANGE_PRICE]["new_price"] == 380000
    assert updates[CHANGE_SOLD]["new_price"] == 375000
    assert updates[CHANGE_SOLD]["source_collection"] == store.settings.sold_collection
    assert updates[CHANGE_NEW]["source_collection"] == store.settings.properties_collection

    property_ids = {doc["property_id"] for doc in updates.values()}
    assert len(property_ids) == 1, "all three changes describe the same property"

    for doc in updates.values():
        assert doc["listing_id"] == "202600001"
        assert doc["address"] == "12 Bluenose Lane, Chester"


def test_a_change_recorded_twice_does_not_duplicate_the_deck(store, client):
    run(client, store, entry("202600001"))
    store.properties.update_one({"listing_id": "202600001"}, {"$set": {"Price": "$400,000"}})

    client.prices["202600001"] = 380000
    run(client, store, entry("202600001", list_price="380000"))
    store.properties.update_one({"listing_id": "202600001"}, {"$set": {"Price": "$400,000"}})
    run(client, store, entry("202600001", list_price="380000"))

    assert store.recent_updates.count_documents({"change_type": CHANGE_PRICE}) == 1


# -- coming back from the archive ---------------------------------------


def test_a_listing_that_returns_under_its_own_id_moves_back(store, client):
    run(client, store, entry("202600001"))
    original_id = store.properties.find_one({"listing_id": "202600001"})["_id"]
    run(client, store, entry("202600001", status_id=SOLD_CLOSED, sold_price="375000"))

    client.statuses["202600001"] = STATUS_FOR_SALE
    revived = run(client, store, entry("202600001", status_id=ACTIVE, list_price="410000"))

    assert revived.new_listings == 1
    assert store.sold.count_documents({"listing_id": "202600001"}) == 0

    back = store.properties.find_one({"listing_id": "202600001"})
    assert back["_id"] == original_id, "the id must survive a return to the market"
    assert back["relisted"] is True
    assert "sold_on" not in back
    assert "sold_at" not in back
    assert "sold_price" not in back


def test_an_archived_listing_reported_sold_again_is_left_alone(store, client):
    run(client, store, entry("202600001"))
    run(client, store, entry("202600001", status_id=SOLD_CLOSED, sold_price="375000"))
    before = store.sold.find_one({"listing_id": "202600001"})

    fetches = len(client.fetches)
    again = run(client, store, entry("202600001", status_id=SOLD_CLOSED, sold_price="375000"))

    assert again.writes == 0
    assert len(client.fetches) == fetches
    assert store.sold.find_one({"listing_id": "202600001"}) == before


def test_a_delisted_listing_leaves_the_map_without_being_called_sold(store, client):
    run(client, store, entry("202600001"))
    delisted = run(client, store, entry("202600001", status_id=EXPIRED))

    assert delisted.delisted == 1
    assert store.properties.count_documents({}) == 0

    archived = store.sold.find_one({"listing_id": "202600001"})
    assert archived["archived_reason"] == "expired"
    assert archived["Status"] != STATUS_SOLD
    assert archived["delisted_at"] is not None
    assert "sold_at" not in archived


# -- relisting detection details ----------------------------------------


def test_relisting_is_matched_on_parcel_id_not_address(store, client):
    run(client, store, entry("202600001"))
    run(client, store, entry("202600001", status_id=SOLD_CLOSED, sold_price="375000"))

    client.pids["202700002"] = "99999999"
    run(client, store, entry("202700002"))

    fresh = store.properties.find_one({"listing_id": "202700002"})
    assert "relisted" not in fresh
    assert "sale_history" not in fresh


def test_relisting_history_lists_the_most_recent_sale_first(store, client):
    now = utcnow()
    for index, days in enumerate((400, 900)):
        store.sold.insert_one(
            {
                "listing_id": f"20240000{index}",
                "PID": SHARED_PID,
                "Status": STATUS_SOLD,
                "sold_at": now - timedelta(days=days),
                "sold_price": 300000 + index,
            }
        )

    run(client, store, entry("202700002"))

    fresh = store.properties.find_one({"listing_id": "202700002"})
    assert fresh["previous_listing_ids"] == ["202400000", "202400001"]
    assert [sale["sold_price"] for sale in fresh["sale_history"]] == [300000, 300001]
