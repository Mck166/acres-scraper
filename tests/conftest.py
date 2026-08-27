"""Shared test fixtures.

Database tests run against a real MongoDB so that unique indexes, TTL indexes,
and upsert semantics behave the way they will in production. They target a
separate database named by MONGODB_TEST_DB_NAME and skip when no server is
reachable.
"""

import json
import os
import pathlib
import sys

import pytest
from bson import json_util

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper.config import Settings, get_settings  # noqa: E402
from scraper.store import Store, connect  # noqa: E402

GOLDEN_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "golden"
FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"

DEFAULT_TEST_DB = "Acres-Props-Test"


def test_db_name() -> str:
    return os.getenv("MONGODB_TEST_DB_NAME", DEFAULT_TEST_DB)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def mongo_client(settings):
    if test_db_name() == settings.mongodb_db_name:
        pytest.fail(
            f"MONGODB_TEST_DB_NAME must differ from MONGODB_DB_NAME "
            f"(both are {settings.mongodb_db_name!r}); tests would destroy production data"
        )

    try:
        client = connect(settings)
    except Exception as exc:
        pytest.skip(f"No MongoDB available for integration tests: {exc}")

    yield client
    client.close()


@pytest.fixture
def store(mongo_client, settings) -> Store:
    """A Store pointed at the test database, emptied before and after."""
    store = Store(mongo_client, settings, db_name=test_db_name())
    _drop_all(store)
    store.ensure_indexes()
    yield store
    _drop_all(store)


def _drop_all(store: Store) -> None:
    for collection in (
        store.properties,
        store.sold,
        store.recent_updates,
        store.geocode_cache,
        store.scrape_runs,
        store.locks,
        store.notification_events,
    ):
        collection.drop()


def _load_golden(name: str) -> dict:
    path = GOLDEN_DIR / f"{name}.json"
    return json_util.loads(path.read_text())


@pytest.fixture(scope="session")
def golden_docs() -> dict:
    """Real production documents, keyed by the shape they represent."""
    if not GOLDEN_DIR.exists():
        pytest.skip("Golden fixtures missing; run tools/snapshot_golden.py")
    return {path.stem: _load_golden(path.stem) for path in sorted(GOLDEN_DIR.glob("*.json"))}


@pytest.fixture(scope="session")
def api_fixtures() -> dict:
    """Recorded viewpoint API responses, keyed by endpoint name."""
    path = FIXTURE_DIR / "viewpoint"
    if not path.exists():
        return {}
    return {p.stem: json.loads(p.read_text()) for p in sorted(path.glob("*.json"))}
