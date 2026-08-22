"""Geocoding, which is now a fallback rather than the main path.

Viewpoint returns coordinates with every listing, so Nominatim is only consulted
when they are missing. It allows about one request per second, so every lookup
is cached, including the ones that fail.
"""

import pytest

from scraper.geocode import CachedGeocoder, address_variants, normalize_address_key, within_nova_scotia
from scraper.sync import enrich_coordinates


class CountingGeocoder(CachedGeocoder):
    """A geocoder whose network call is replaced by a canned answer."""

    def __init__(self, cache, result=None):
        super().__init__(cache)
        self.result = result
        self.lookups = 0

    def _resolve(self, address):
        self.lookups += 1
        return self.result


@pytest.fixture(autouse=True)
def stub_network(monkeypatch):
    """Make sure no test in this module can reach Nominatim."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to call Nominatim")

    monkeypatch.setattr("scraper.geocode.geocode_address", refuse)


def test_normalize_address_key_collapses_punctuation_and_case():
    assert normalize_address_key("42 Oldham Road, Enfield") == "42 oldham road enfield"
    assert normalize_address_key("42  OLDHAM   ROAD,, Enfield.") == "42 oldham road enfield"
    assert normalize_address_key("") == ""


def test_address_variants_get_progressively_looser():
    variants = address_variants("42 Oldham Road, Enfield")
    assert variants[0] == "42 Oldham Road, Enfield, Nova Scotia, Canada"
    assert variants[-1] == "Enfield, Nova Scotia, Canada"
    assert address_variants("") == []


def test_within_nova_scotia_bounds():
    assert within_nova_scotia(44.65, -63.57)
    assert not within_nova_scotia(43.65, -79.38)
    assert not within_nova_scotia(0, 0)


def test_lookup_is_cached_across_calls(store):
    geocoder = CountingGeocoder(
        store.geocode_cache,
        result={"latitude": 44.65, "longitude": -63.57, "display_name": "Halifax"},
    )

    first = geocoder.lookup("1 Barrington Street, Halifax")
    second = geocoder.lookup("1 Barrington Street, Halifax")

    assert first == second
    assert geocoder.lookups == 1, "the second lookup should have come from the cache"


def test_cache_key_ignores_formatting_differences(store):
    geocoder = CountingGeocoder(
        store.geocode_cache,
        result={"latitude": 44.65, "longitude": -63.57, "display_name": "Halifax"},
    )

    geocoder.lookup("1 Barrington Street, Halifax")
    geocoder.lookup("1  BARRINGTON  STREET,  Halifax")

    assert geocoder.lookups == 1


def test_failures_are_cached_too(store):
    """An address that cannot be resolved must not be retried every run."""
    geocoder = CountingGeocoder(store.geocode_cache, result=None)

    assert geocoder.lookup("Nowhere At All") is None
    assert geocoder.lookup("Nowhere At All") is None
    assert geocoder.lookups == 1


def test_empty_address_is_not_looked_up(store):
    geocoder = CountingGeocoder(store.geocode_cache, result=None)
    assert geocoder.lookup("") is None
    assert geocoder.lookups == 0


def test_coordinates_from_the_listing_are_left_alone(store):
    """Geocoding is a fallback; supplied coordinates win."""
    geocoder = CountingGeocoder(
        store.geocode_cache, result={"latitude": 0.1, "longitude": 0.2, "display_name": "wrong"}
    )
    document = {"Address": "42 Oldham Road, Enfield", "latitude": 44.9, "longitude": -63.5}

    enriched = enrich_coordinates(document, geocoder)

    assert enriched["latitude"] == 44.9
    assert enriched["longitude"] == -63.5
    assert geocoder.lookups == 0


def test_missing_coordinates_fall_back_to_geocoding(store):
    geocoder = CountingGeocoder(
        store.geocode_cache,
        result={"latitude": 44.9, "longitude": -63.5, "display_name": "Enfield, NS"},
    )
    document = {"Address": "42 Oldham Road, Enfield", "latitude": None, "longitude": None}

    enriched = enrich_coordinates(document, geocoder)

    assert enriched["latitude"] == 44.9
    assert enriched["longitude"] == -63.5
    assert enriched["geocoded_address"] == "Enfield, NS"


def test_a_listing_without_an_address_is_left_alone(store):
    geocoder = CountingGeocoder(store.geocode_cache, result=None)
    document = {"Address": "", "latitude": None, "longitude": None}

    enriched = enrich_coordinates(document, geocoder)

    assert enriched["latitude"] is None
    assert geocoder.lookups == 0
