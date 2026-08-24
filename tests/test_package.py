"""Phase 0: the refactored package is importable and wired together correctly."""

import importlib

import pytest

MODULES = [
    "scraper",
    "scraper.config",
    "scraper.identity",
    "scraper.normalize",
    "scraper.photos",
    "scraper.geocode",
    "scraper.store",
    "scraper.sync",
    "scraper.selenium_client",
    "scraper.run",
    "scraper.inventory",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name):
    assert importlib.import_module(module_name) is not None


def test_entrypoint_shim_exposes_main():
    """Docker and cron both invoke app.py, so it has to keep working."""
    module = importlib.import_module("app")
    assert callable(module.main)


def test_settings_have_the_new_collection_names(settings):
    assert settings.properties_collection
    assert settings.sold_collection != settings.properties_collection
    assert settings.recent_updates_collection not in (
        settings.properties_collection,
        settings.sold_collection,
    )


def test_golden_fixtures_cover_the_interesting_shapes(golden_docs):
    assert set(golden_docs) >= {"for_sale", "sold", "missing_photos", "no_coordinates"}
    assert golden_docs["sold"]["Status"] == "SOLD"
    assert len(golden_docs["missing_photos"].get("Photos") or []) <= 1


def test_store_targets_the_test_database(store, settings):
    assert store.db.name != settings.mongodb_db_name
    assert store.properties.count_documents({}) == 0
