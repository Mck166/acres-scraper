"""Migration gate: the one-off pass over the data that already exists.

The production collection predates every convention the scraper now relies on:
no `listing_id`, sold homes sitting in the active collection, and statuses like
`NEW PRICE` that the map's substring matching silently drops. This is the pass
that fixes all of it, and it has to be safe to run twice.
"""

import pathlib

import pytest
from bson import ObjectId

from scraper.normalize import STATUS_FOR_SALE, STATUS_PENDING, STATUS_SOLD, utcnow
from scraper.backfill import restore_misarchived_pending
from tools.migrate import (
    backup,
    duplicate_listing_ids,
    migrate,
    renormalize,
    restore,
    status_breakdown,
)


def legacy(url="https://www.viewpoint.ca/cutsheet/202500001/1", status="FOR SALE", **extra):
    """A document shaped the way the old scraper wrote them."""
    doc = {
        "_id": ObjectId(),
        "url": url,
        "Address": "1 Old Road, Halifax",
        "Status": status,
        "Price": "$425,000",
        "Photos": [f"{url}/photo.jpg"],
        "Photo_Count": 1,
        "BEDS": "3",
        "PID": "40123456",
        "latitude": 44.65,
        "longitude": -63.57,
        "date_added": utcnow(),
        "date_updated": utcnow(),
    }
    doc.update(extra)
    return doc


def run(store, **kwargs):
    kwargs.setdefault("skip_photos", True)
    return migrate(store, **kwargs)


def store_and_read(collection, doc):
    """Insert a document and hand back what MongoDB actually kept.

    Mongo truncates datetimes to milliseconds, so the dict we passed in is not
    the document that exists, and comparing against it would test nothing real.
    """
    collection.insert_one(doc)
    return collection.find_one({"_id": doc["_id"]})


# -- backup ---------------------------------------------------------------


def test_a_backup_round_trips_every_document(store, tmp_path):
    stored = store_and_read(store.properties, legacy())

    destination = backup(store, root=pathlib.Path(tmp_path))
    store.properties.delete_many({})

    restore(store, destination)

    recovered = store.properties.find_one({"_id": stored["_id"]})
    assert recovered == stored, "the backup did not restore the document unchanged"


def test_the_backup_keeps_its_object_ids_and_dates(store, tmp_path):
    stored = store_and_read(store.properties, legacy())

    destination = backup(store, root=pathlib.Path(tmp_path))
    store.properties.delete_many({})
    restore(store, destination)

    recovered = store.properties.find_one({})
    assert isinstance(recovered["_id"], ObjectId)
    assert recovered["date_added"] == stored["date_added"]


# -- the guard ------------------------------------------------------------


def test_two_documents_sharing_a_listing_id_stop_the_migration(store):
    url = "https://www.viewpoint.ca/cutsheet/202500001/1"
    store.properties.insert_many([legacy(url=url), legacy(url=url)])

    results = run(store, apply_changes=True)

    assert results["duplicate_listing_ids"] == {"202500001": 2}
    assert "archived" not in results, "the migration carried on past a blocker"
    assert store.sold.count_documents({}) == 0


def test_a_clean_database_has_no_duplicates(store):
    store.properties.insert_many(
        [
            legacy(url="https://www.viewpoint.ca/cutsheet/202500001/1"),
            legacy(url="https://www.viewpoint.ca/cutsheet/202500002/1"),
        ]
    )

    assert duplicate_listing_ids(store.properties) == {}


# -- normalization --------------------------------------------------------


def test_a_new_price_listing_becomes_one_the_map_will_draw(store):
    """`NEW PRICE` contains none of the tokens the clients match on."""
    store.properties.insert_one(legacy(status="NEW PRICE"))

    renormalize(store.properties, dry_run=False)

    doc = store.properties.find_one({})
    assert doc["Status"] == STATUS_FOR_SALE
    assert doc["status_raw"] == "NEW PRICE", "the site's own wording was thrown away"


def test_normalization_fills_in_what_the_new_code_expects(store):
    store.properties.insert_one(legacy())

    renormalize(store.properties, dry_run=False)

    doc = store.properties.find_one({})
    assert doc["listing_id"] == "202500001"
    assert doc["listing_class_id"] == "1"
    assert doc["price_value"] == 425000


def test_normalization_keeps_every_field_the_clients_read(store):
    original = store_and_read(store.properties, legacy())

    renormalize(store.properties, dry_run=False)

    doc = store.properties.find_one({})
    for key in ("Address", "BEDS", "PID", "latitude", "longitude", "date_added", "Photos"):
        assert doc[key] == original[key], f"{key} was lost"


def test_a_dry_run_writes_nothing(store):
    store.properties.insert_one(legacy(status="NEW PRICE"))

    summary = renormalize(store.properties, dry_run=True)

    assert summary["changed"] == 1
    assert store.properties.find_one({})["Status"] == "NEW PRICE"


# -- the whole pass -------------------------------------------------------


def test_the_migration_sorts_the_collection_out(store):
    store.properties.insert_many(
        [
            legacy(url="https://www.viewpoint.ca/cutsheet/202500001/1", status="FOR SALE"),
            legacy(url="https://www.viewpoint.ca/cutsheet/202500002/1", status="NEW PRICE"),
            legacy(url="https://www.viewpoint.ca/cutsheet/202500003/1", status="SOLD"),
            legacy(url="https://www.viewpoint.ca/cutsheet/202500004/5", status="PENDING"),
        ]
    )

    results = run(store, apply_changes=True)

    assert results["archived"]["archived"] == 1
    assert store.properties.count_documents({}) == 3
    assert store.sold.count_documents({}) == 1

    assert status_breakdown(store.properties) == {STATUS_FOR_SALE: 2, "PENDING SALE": 1}
    assert store.sold.find_one({})["archived_reason"] == "sold"


def test_a_favourited_home_that_had_already_sold_still_resolves(store):
    doc = legacy(status="SOLD")
    store.properties.insert_one(doc)
    favourite = str(doc["_id"])

    run(store, apply_changes=True)

    archived = store.sold.find_one({"listing_id": "202500001"})
    assert str(archived["_id"]) == favourite
    assert archived["sold_price"] == 425000
    assert archived["sold_at"] is not None


def test_running_the_migration_twice_changes_nothing_the_second_time(store):
    store.properties.insert_many(
        [
            legacy(url="https://www.viewpoint.ca/cutsheet/202500001/1", status="NEW PRICE"),
            legacy(url="https://www.viewpoint.ca/cutsheet/202500002/1", status="SOLD"),
        ]
    )

    run(store, apply_changes=True)
    before = sorted(store.properties.find({}), key=lambda d: d["_id"])

    second = run(store, apply_changes=True)

    assert second["normalized"]["changed"] == 0
    assert second["archived"]["archived"] == 0
    assert second["listing_ids"]["updated"] == 0
    assert sorted(store.properties.find({}), key=lambda d: d["_id"]) == before


def test_the_migration_leaves_the_indexes_the_scraper_needs(store):
    store.properties.insert_one(legacy())

    run(store, apply_changes=True)

    indexes = store.properties.index_information()
    keyed = {tuple(spec["key"])[0][0]: spec for spec in indexes.values()}
    assert keyed["listing_id"].get("unique") is True

    ttl = [
        spec
        for spec in store.recent_updates.index_information().values()
        if "expireAfterSeconds" in spec
    ]
    assert ttl and ttl[0]["expireAfterSeconds"] == 86400


def test_an_already_archived_sale_is_not_archived_again(store):
    store.sold.insert_one(legacy(status=STATUS_SOLD, sold_at=utcnow()))
    store.properties.insert_one(legacy(url="https://www.viewpoint.ca/cutsheet/202500002/1"))

    results = run(store, apply_changes=True)

    assert results["archived"]["archived"] == 0
    assert store.sold.count_documents({}) == 1


def test_a_pending_listing_archived_as_sold_is_restored(store):
    store.sold.insert_one(
        legacy(
            status=STATUS_SOLD,
            status_id="6",
            sold_on=utcnow(),
            sold_at=utcnow(),
            archived_at=utcnow(),
            archived_reason="sold",
            source_updated_at=utcnow(),
        )
    )

    summary = restore_misarchived_pending(store, dry_run=False)

    assert summary["restored"] == 1
    assert store.sold.count_documents({}) == 0
    restored = store.properties.find_one({})
    assert restored["Status"] == STATUS_PENDING
    assert restored["status_id"] == "6"
    assert restored.get("pending_on")
    assert "sold_on" not in restored
    assert "sold_at" not in restored
    assert "archived_reason" not in restored
