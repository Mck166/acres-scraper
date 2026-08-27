"""MongoDB access layer.

Three collections carry listing data:

``properties``      active listings, the source of truth for the feed and map
``sold_properties`` sold listings, archived with their original ``_id`` so that
                    favourites and dislikes keep resolving
``recent_updates``  one row per change in the last 24 hours, expired by a TTL
                    index, used to drive the app's swipe deck
``notification_events`` price/pending/sold rows for iOS favourite alerts
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import certifi
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

from .config import Settings, get_settings
from .identity import resolve_listing_id
from .normalize import (
    STATUS_EXPIRED,
    STATUS_SOLD,
    atlantic_today,
    listing_is_off_market,
    listing_is_pending,
    listing_is_sold,
    parse_price,
    utcnow,
)

CHANGE_NEW = "new"
CHANGE_PRICE = "price"
CHANGE_PENDING = "pending"
CHANGE_SOLD = "sold"
CHANGE_DELISTED = "delisted"
CHANGE_NONE = "none"

# Changes worth telling the app about. A delisting is not one of them: there is
# nothing for a user to swipe on, the listing simply stops appearing.
NOTIFIABLE_CHANGES = (CHANGE_NEW, CHANGE_PRICE, CHANGE_SOLD)

# Favourite push alerts. Pending stays off the swipe deck but still notifies
# anyone who saved the listing.
PUSH_CHANGES = (CHANGE_PRICE, CHANGE_PENDING, CHANGE_SOLD)


@dataclass
class SaveResult:
    """What happened to a single listing during a save."""

    listing_id: Optional[str]
    action: str
    property_id: Any = None
    old_price: Optional[float] = None
    new_price: Optional[float] = None
    relisted: bool = False

    @property
    def is_change(self) -> bool:
        return self.action != CHANGE_NONE


@dataclass
class RunSummary:
    """Counters for a single scraper run."""

    started_at: datetime = field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    listings_seen: int = 0
    new_listings: int = 0
    price_changes: int = 0
    sold: int = 0
    pending: int = 0
    delisted: int = 0
    unchanged: int = 0
    relisted: int = 0
    skipped: int = 0
    detail_fetches: int = 0
    stale_rechecked: int = 0
    errors: List[str] = field(default_factory=list)

    def record(self, result: SaveResult, seen: bool = True) -> None:
        """Count an outcome.

        `seen` is false for the stale re-check, which revisits listings we
        already hold rather than seeing them on the activity feed.
        """
        if seen:
            self.listings_seen += 1
        if result.action == CHANGE_NEW:
            self.new_listings += 1
        elif result.action == CHANGE_PRICE:
            self.price_changes += 1
        elif result.action == CHANGE_SOLD:
            self.sold += 1
        elif result.action == CHANGE_PENDING:
            self.pending += 1
        elif result.action == CHANGE_DELISTED:
            self.delisted += 1
        else:
            self.unchanged += 1
        if result.relisted:
            self.relisted += 1

    @property
    def writes(self) -> int:
        """How many listings this run actually changed."""
        return (
            self.new_listings
            + self.price_changes
            + self.sold
            + self.pending
            + self.delisted
        )

    def to_document(self) -> Dict[str, Any]:
        finished = self.finished_at or utcnow()
        return {
            "started_at": self.started_at,
            "finished_at": finished,
            "duration_seconds": (finished - self.started_at).total_seconds(),
            "listings_seen": self.listings_seen,
            "new_listings": self.new_listings,
            "price_changes": self.price_changes,
            "sold": self.sold,
            "pending": self.pending,
            "delisted": self.delisted,
            "unchanged": self.unchanged,
            "relisted": self.relisted,
            "skipped": self.skipped,
            "detail_fetches": self.detail_fetches,
            "stale_rechecked": self.stale_rechecked,
            "errors": self.errors,
        }


def connect(settings: Optional[Settings] = None) -> MongoClient:
    settings = settings or get_settings()

    options: Dict[str, Any] = {"serverSelectionTimeoutMS": 10000}
    # Python installs on macOS routinely lack a usable system CA bundle, which
    # makes Atlas connections fail TLS verification. Point pymongo at certifi's.
    if settings.mongodb_uri.startswith("mongodb+srv://") or "tls=true" in settings.mongodb_uri.lower():
        options["tlsCAFile"] = certifi.where()

    client = MongoClient(settings.mongodb_uri, **options)
    client.admin.command("ping")
    return client


class Store:
    """Everything the scraper does to MongoDB."""

    def __init__(self, client: MongoClient, settings: Optional[Settings] = None, db_name: Optional[str] = None):
        self.settings = settings or get_settings()
        self.client = client
        self.db = client[db_name or self.settings.mongodb_db_name]

    # -- collections -----------------------------------------------------

    @property
    def properties(self):
        return self.db[self.settings.properties_collection]

    @property
    def sold(self):
        return self.db[self.settings.sold_collection]

    @property
    def recent_updates(self):
        return self.db[self.settings.recent_updates_collection]

    @property
    def notification_events(self):
        return self.db[self.settings.notification_events_collection]

    @property
    def geocode_cache(self):
        return self.db[self.settings.geocode_cache_collection]

    @property
    def scrape_runs(self):
        return self.db[self.settings.scrape_runs_collection]

    @property
    def locks(self):
        return self.db[self.settings.locks_collection]

    # -- schema ----------------------------------------------------------

    def _ensure_ttl_index(self, collection, field: str, expire_seconds: int) -> None:
        """Create a TTL index, replacing one that exists with different options."""
        name = f"{field}_1"
        existing = collection.index_information().get(name)
        if existing is not None and existing.get("expireAfterSeconds") != expire_seconds:
            collection.drop_index(name)
        collection.create_index(field, expireAfterSeconds=expire_seconds)

    def ensure_indexes(self) -> None:
        for collection in (self.properties, self.sold):
            collection.create_index("listing_id", unique=True, sparse=True)
            collection.create_index("PID")
            collection.create_index("Status")
            collection.create_index("url")
            collection.create_index("date_added")
            collection.create_index("date_updated")
            collection.create_index("listed_on")
            collection.create_index("price_changed_on")
            collection.create_index("pending_on")
            collection.create_index("sold_on")

        self._ensure_ttl_index(
            self.recent_updates, "changed_at", self.settings.recent_updates_ttl_seconds
        )
        self.recent_updates.create_index("property_id")
        self.recent_updates.create_index("listing_id")
        self.notification_events.create_index(
            [("property_id", ASCENDING), ("change_type", ASCENDING), ("event_day", ASCENDING)],
            unique=True,
        )
        self.notification_events.create_index("processed")
        self.sold.create_index("sold_at")
        self._ensure_ttl_index(self.locks, "expires_at", 0)

    # -- lookups ---------------------------------------------------------

    def find_active(self, listing_id: str, url: Optional[str] = None) -> Optional[dict]:
        return self._find_in(self.properties, listing_id, url)

    def find_sold(self, listing_id: str, url: Optional[str] = None) -> Optional[dict]:
        return self._find_in(self.sold, listing_id, url)

    @staticmethod
    def _find_in(collection, listing_id: Optional[str], url: Optional[str]) -> Optional[dict]:
        clauses: List[dict] = []
        if listing_id:
            clauses.append({"listing_id": str(listing_id)})
        if url:
            clauses.append({"url": url})
        if not clauses:
            return None
        query = clauses[0] if len(clauses) == 1 else {"$or": clauses}
        return collection.find_one(query)

    SNAPSHOT_PROJECTION = {
        "listing_id": 1,
        "url": 1,
        "Status": 1,
        "Price": 1,
        "price_value": 1,
        "PID": 1,
        "date_added": 1,
        "date_updated": 1,
    }

    def active_snapshot(self) -> Dict[str, dict]:
        """Lightweight view of every active listing, keyed by listing id."""
        return self._snapshot(self.properties)

    def archived_snapshot(self) -> Dict[str, dict]:
        """Lightweight view of every archived listing, keyed by listing id."""
        return self._snapshot(self.sold)

    def _snapshot(self, collection) -> Dict[str, dict]:
        snapshot: Dict[str, dict] = {}
        for doc in collection.find({}, self.SNAPSHOT_PROJECTION):
            listing_id = resolve_listing_id(doc)
            if listing_id:
                snapshot[listing_id] = doc
        return snapshot

    def stale_active_listings(self, limit: int) -> List[dict]:
        """The active listings we have not looked at in the longest."""
        if limit <= 0:
            return []
        cursor = (
            self.properties.find({}, {"listing_id": 1, "url": 1, "Status": 1, "Price": 1, "date_updated": 1})
            .sort("date_updated", ASCENDING)
            .limit(limit)
        )
        return list(cursor)

    # -- writes ----------------------------------------------------------

    def save_listing(self, document: Dict[str, Any]) -> SaveResult:
        """Insert, update, or archive a listing based on how it changed."""
        document = dict(document)
        listing_id = resolve_listing_id(document)
        url = document.get("url")
        now = utcnow()

        existing = self.find_active(listing_id, url) if listing_id or url else None
        sold_now = listing_is_sold(document)

        if existing is None:
            archived = self.find_sold(listing_id, url) if listing_id or url else None
            if archived is not None:
                return self._refresh_archived(archived, document, now)
            return self._insert_new(document, now, sold_now)

        return self._update_existing(existing, document, now, sold_now)

    def _insert_new(self, document: Dict[str, Any], now: datetime, sold_now: bool) -> SaveResult:
        document.setdefault("date_added", now)
        document["date_updated"] = now

        relisted = self._apply_relisting_history(document)

        collection = self.sold if sold_now else self.properties
        if sold_now:
            document.setdefault("sold_at", now)
            document.setdefault("sold_price", parse_price(document.get("Price")))
            document["archived_at"] = now
            document.setdefault("archived_reason", "sold")

        try:
            result = collection.insert_one(document)
            property_id = result.inserted_id
        except DuplicateKeyError:
            # Another run inserted it first; fold our data into that document.
            existing = self._find_in(collection, resolve_listing_id(document), document.get("url"))
            if existing is None:
                raise
            collection.replace_one(
                {"_id": existing["_id"]},
                {**existing, **{k: v for k, v in document.items() if k != "_id"}, "_id": existing["_id"]},
            )
            property_id = existing["_id"]

        return SaveResult(
            listing_id=resolve_listing_id(document),
            action=CHANGE_SOLD if sold_now else CHANGE_NEW,
            property_id=property_id,
            # What a sold card should say is what it sold for, not what it asked.
            new_price=document["sold_price"] if sold_now else parse_price(document.get("Price")),
            relisted=relisted,
        )

    def _update_existing(
        self, existing: dict, document: Dict[str, Any], now: datetime, sold_now: bool
    ) -> SaveResult:
        old_price = parse_price(existing.get("Price"))
        new_price = parse_price(document.get("Price"))
        was_sold = listing_is_sold(existing)

        update: Dict[str, Any] = {key: value for key, value in document.items() if key != "_id"}
        update.pop("date_added", None)
        update["date_updated"] = now

        price_changed = (
            old_price is not None and new_price is not None and old_price != new_price
        )
        if price_changed:
            update["price_changed_at"] = now
            if not document.get("price_changed_on"):
                update["price_changed_on"] = now
            if not document.get("price_history"):
                history = list(existing.get("price_history") or [])
                history.append(
                    {
                        "price": existing.get("Price"),
                        "price_value": old_price,
                        "date": existing.get("price_changed_on")
                        or existing.get("date_updated")
                        or existing.get("date_added")
                        or now,
                    }
                )
                update["price_history"] = history

        if listing_is_off_market(document) and not was_sold:
            reason = "sold" if sold_now else "expired"
            return self._archive(existing, update, now, old_price, new_price, reason=reason)

        merged = dict(existing)
        merged.update(update)
        merged["_id"] = existing["_id"]
        self.properties.replace_one({"_id": existing["_id"]}, merged)

        became_pending = listing_is_pending(merged) and not listing_is_pending(existing)
        if price_changed:
            action = CHANGE_PRICE
        elif became_pending:
            action = CHANGE_PENDING
        else:
            action = CHANGE_NONE

        return SaveResult(
            listing_id=resolve_listing_id(existing) or resolve_listing_id(document),
            action=action,
            property_id=existing["_id"],
            old_price=old_price,
            new_price=new_price,
        )

    def _archive(
        self,
        existing: dict,
        update: Dict[str, Any],
        now: datetime,
        old_price: Optional[float],
        new_price: Optional[float],
        reason: str = "sold",
    ) -> SaveResult:
        """Move a listing out of the active collection, keeping its ``_id``.

        The id is preserved because ``favorites`` and ``dislikes`` reference
        properties by it; changing it would orphan every saved home.
        """
        archived = dict(existing)
        archived.update(update)
        archived["_id"] = existing["_id"]
        archived["archived_at"] = now
        archived["archived_reason"] = reason

        if reason == "sold":
            archived["Status"] = STATUS_SOLD
            archived["sold_at"] = now
            if archived.get("sold_price") is None:
                archived["sold_price"] = new_price if new_price is not None else old_price
        else:
            archived["Status"] = STATUS_EXPIRED
            archived["delisted_at"] = now

        date_added = existing.get("date_added")
        if isinstance(date_added, datetime):
            archived["days_on_market"] = max((now - date_added).days, 0)

        self.sold.replace_one({"_id": existing["_id"]}, archived, upsert=True)
        self.properties.delete_one({"_id": existing["_id"]})

        return SaveResult(
            listing_id=resolve_listing_id(archived),
            action=CHANGE_SOLD if reason == "sold" else CHANGE_DELISTED,
            property_id=existing["_id"],
            old_price=old_price,
            new_price=archived.get("sold_price"),
        )

    def archive_listing(
        self,
        existing: dict,
        reason: str = "sold",
        sold_price: Optional[float] = None,
        price: Optional[str] = None,
    ) -> SaveResult:
        """Retire a listing we already hold, without re-fetching it.

        Used when the activity feed reports a sale or a delisting: everything
        needed is already stored, so only the outcome is applied.
        """
        update: Dict[str, Any] = {"date_updated": utcnow()}
        if price:
            update["Price"] = price
        if sold_price is not None:
            update["sold_price"] = sold_price

        return self._archive(
            existing,
            update,
            utcnow(),
            old_price=parse_price(existing.get("Price")),
            new_price=sold_price,
            reason=reason,
        )

    def _refresh_archived(self, archived: dict, document: Dict[str, Any], now: datetime) -> SaveResult:
        """A listing we already archived showed up again under the same id."""
        if not listing_is_sold(document):
            # It came back on the market under its original listing id, so move
            # it back into the active collection rather than creating a twin.
            revived = dict(archived)
            revived.update({key: value for key, value in document.items() if key != "_id"})
            revived["_id"] = archived["_id"]
            revived["date_updated"] = now
            revived["relisted"] = True
            revived["relisted_at"] = now
            for key in (
                "sold_on",
                "sold_at",
                "sold_price",
                "archived_at",
                "archived_reason",
                "days_on_market",
            ):
                revived.pop(key, None)

            self.properties.replace_one({"_id": archived["_id"]}, revived, upsert=True)
            self.sold.delete_one({"_id": archived["_id"]})

            return SaveResult(
                listing_id=resolve_listing_id(revived),
                action=CHANGE_NEW,
                property_id=archived["_id"],
                new_price=parse_price(revived.get("Price")),
                relisted=True,
            )

        update = {key: value for key, value in document.items() if key != "_id"}
        update.pop("date_added", None)
        update["date_updated"] = now
        merged = dict(archived)
        merged.update(update)
        merged["_id"] = archived["_id"]
        self.sold.replace_one({"_id": archived["_id"]}, merged)
        return SaveResult(
            listing_id=resolve_listing_id(archived),
            action=CHANGE_NONE,
            property_id=archived["_id"],
        )

    def _apply_relisting_history(self, document: Dict[str, Any]) -> bool:
        """Link a new listing to earlier sales of the same parcel."""
        pid = document.get("PID")
        if not pid:
            return False

        listing_id = resolve_listing_id(document)
        previous = list(
            self.sold.find(
                {"PID": str(pid)},
                {"listing_id": 1, "sold_at": 1, "sold_price": 1, "Price": 1, "url": 1},
            ).sort("sold_at", DESCENDING)
        )
        previous = [doc for doc in previous if resolve_listing_id(doc) != listing_id]
        if not previous:
            return False

        document["relisted"] = True
        document["previous_listing_ids"] = [
            resolve_listing_id(doc) for doc in previous if resolve_listing_id(doc)
        ]
        document["sale_history"] = [
            {
                "listing_id": resolve_listing_id(doc),
                "sold_at": doc.get("sold_at"),
                "sold_price": doc.get("sold_price") or parse_price(doc.get("Price")),
            }
            for doc in previous
        ]
        return True

    # -- change log ------------------------------------------------------

    def record_change(self, result: SaveResult, document: Dict[str, Any]) -> None:
        """Log a change so the app's swipe deck can find it for 24 hours."""
        if result.action not in NOTIFIABLE_CHANGES:
            return

        source = self.settings.sold_collection if result.action == CHANGE_SOLD else self.settings.properties_collection
        self.recent_updates.update_one(
            {"property_id": str(result.property_id), "change_type": result.action},
            {
                "$set": {
                    "property_id": str(result.property_id),
                    "listing_id": result.listing_id,
                    "change_type": result.action,
                    "old_price": result.old_price,
                    "new_price": result.new_price,
                    "address": document.get("Address"),
                    "source_collection": source,
                    "changed_at": utcnow(),
                }
            },
            upsert=True,
        )

    def record_push_event(
        self,
        result: SaveResult,
        document: Dict[str, Any],
        action: Optional[str] = None,
    ) -> None:
        """Queue a favourite push for price, pending, and sold changes.

        Pending is deliberately excluded from ``recent_updates`` (nothing to
        swipe on) but still belongs in this collection.
        """
        kind = action or result.action
        if kind not in PUSH_CHANGES or result.property_id is None:
            return

        event_day = atlantic_today().isoformat()
        self.notification_events.update_one(
            {
                "property_id": str(result.property_id),
                "change_type": kind,
                "event_day": event_day,
            },
            {
                "$setOnInsert": {
                    "property_id": str(result.property_id),
                    "change_type": kind,
                    "event_day": event_day,
                    "created_at": utcnow(),
                    "processed": False,
                },
                "$set": {
                    "address": document.get("Address"),
                    "listing_id": result.listing_id,
                },
            },
            upsert=True,
        )

    # -- run bookkeeping -------------------------------------------------

    def acquire_lock(self, name: str, ttl_seconds: int = 3600) -> bool:
        """Take a run lock. Returns False when another run already holds it."""
        now = utcnow()
        self.locks.delete_many({"_id": name, "expires_at": {"$lte": now}})
        try:
            self.locks.insert_one(
                {"_id": name, "acquired_at": now, "expires_at": now + timedelta(seconds=ttl_seconds)}
            )
            return True
        except DuplicateKeyError:
            return False

    def refresh_lock(self, name: str, ttl_seconds: int = 3600) -> bool:
        """Extend a held lock. Re-acquires if the TTL already dropped it."""
        now = utcnow()
        result = self.locks.update_one(
            {"_id": name},
            {"$set": {"expires_at": now + timedelta(seconds=ttl_seconds)}},
        )
        if result.matched_count:
            return True
        return self.acquire_lock(name, ttl_seconds)

    def release_lock(self, name: str) -> None:
        self.locks.delete_one({"_id": name})

    def record_run(self, summary: RunSummary) -> None:
        self.scrape_runs.insert_one(summary.to_document())
