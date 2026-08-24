"""Offline tests for the description backfill.

These never talk to viewpoint. A fake client returns cutsheet JSON or HTML.
"""

from bson import ObjectId

from scraper.backfill import backfill_descriptions, description_for_listing


CUTSHEET_HTML = """
<html><body>
  <span class="full-description">A waterfront lot with western sunsets.</span>
</body></html>
"""


class FakeClient:
    def __init__(self, descriptions=None, pages=None, fail=None):
        self.descriptions = descriptions or {}
        self.pages = pages or {}
        self.fail = set(fail or ())
        self.calls = []

    def listing_cutsheet(self, listing_id, class_id="1"):
        self.calls.append(("cutsheet", str(listing_id), str(class_id)))
        if str(listing_id) in self.fail:
            raise RuntimeError("cutsheet failed")
        return {
            "status": "success",
            "cutsheet": {"description": self.descriptions.get(str(listing_id))},
        }

    def fetch_page(self, url):
        self.calls.append(("page", url))
        if url in self.fail:
            raise RuntimeError("page failed")
        return self.pages.get(url, "")


def _insert(collection, listing_id, **fields):
    doc = {
        "_id": ObjectId(),
        "listing_id": listing_id,
        "listing_class_id": "1",
        "url": f"https://www.viewpoint.ca/cutsheet/{listing_id}/1",
    }
    doc.update(fields)
    collection.insert_one(doc)
    return doc


def test_description_for_listing_prefers_the_json_api():
    client = FakeClient(descriptions={"202600001": "From the API."})
    doc = {
        "listing_id": "202600001",
        "listing_class_id": "1",
        "url": "https://www.viewpoint.ca/cutsheet/202600001/1",
    }

    assert description_for_listing(client, doc) == "From the API."
    assert client.calls == [("cutsheet", "202600001", "1")]


def test_description_for_listing_falls_back_to_html():
    url = "https://www.viewpoint.ca/cutsheet/202600002/1"
    client = FakeClient(descriptions={"202600002": ""}, pages={url: CUTSHEET_HTML})
    doc = {"listing_id": "202600002", "url": url}

    assert description_for_listing(client, doc) == "A waterfront lot with western sunsets."
    assert ("page", url) in client.calls


def test_backfill_writes_description(store):
    _insert(store.properties, "202600001")
    client = FakeClient(descriptions={"202600001": "Life is a beach!!"})

    summary = backfill_descriptions(client, store.properties)

    assert summary == {
        "examined": 1,
        "updated": 1,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
    }
    stored = store.properties.find_one({"listing_id": "202600001"})
    assert stored["Description"] == "Life is a beach!!"
    assert stored.get("date_updated") is None


def test_backfill_dry_run_writes_nothing(store):
    _insert(store.properties, "202600001")
    client = FakeClient(descriptions={"202600001": "Life is a beach!!"})

    summary = backfill_descriptions(client, store.properties, dry_run=True)

    assert summary["updated"] == 1
    stored = store.properties.find_one({"listing_id": "202600001"})
    assert "Description" not in stored


def test_backfill_resume_skips_listings_that_already_have_one(store):
    _insert(store.properties, "202600001", Description="Already stored.")
    _insert(store.properties, "202600002")
    client = FakeClient(
        descriptions={"202600001": "New copy.", "202600002": "Second listing."}
    )

    summary = backfill_descriptions(client, store.properties, resume=True)

    assert summary["examined"] == 1
    assert summary["updated"] == 1
    assert store.properties.find_one({"listing_id": "202600001"})["Description"] == "Already stored."
    assert store.properties.find_one({"listing_id": "202600002"})["Description"] == "Second listing."


def test_backfill_counts_identical_text_as_unchanged(store):
    _insert(store.properties, "202600001", Description="Same text.")
    client = FakeClient(descriptions={"202600001": "Same text."})

    summary = backfill_descriptions(client, store.properties)

    assert summary["unchanged"] == 1
    assert summary["updated"] == 0


def test_backfill_counts_missing_text_as_failed(store):
    _insert(store.properties, "202600001")
    client = FakeClient(descriptions={"202600001": ""}, pages={})

    summary = backfill_descriptions(client, store.properties)

    assert summary["failed"] == 1
    assert store.properties.find_one({"listing_id": "202600001"}).get("Description") is None


def test_backfill_does_not_touch_photos_or_price(store):
    _insert(
        store.properties,
        "202600001",
        Price="$100,000",
        Photos=["https://example.com/1.jpg"],
        Photo_Count=1,
    )
    client = FakeClient(descriptions={"202600001": "A cottage."})

    backfill_descriptions(client, store.properties)

    stored = store.properties.find_one({"listing_id": "202600001"})
    assert stored["Price"] == "$100,000"
    assert stored["Photos"] == ["https://example.com/1.jpg"]
    assert stored["Photo_Count"] == 1
    assert stored["Description"] == "A cottage."
