"""Parsing a cutsheet page.

The fixture is a real page for MLS 202603269: a sold 4-bed in Baddeck with 49
photos. It exercises the two places listing data hides in the markup.
"""

import pathlib

import pytest

from scraper.cutsheet import (
    description_from_api,
    parse_bootstrap,
    parse_cutsheet,
    parse_description,
    parse_detail_items,
    parse_overlay,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "pages" / "cutsheet_202603269_49photos.html"

# Everything the app's detail screen and the website's detail page read.
REQUIRED_DETAIL_FIELDS = {
    "PID",
    "TYPE",
    "STYLE",
    "BUILDING STYLE",
    "BUILDING DIMENSIONS",
    "AGE",
    "BEDS",
    "BATHROOMS (F/H)",
    "MAIN LIVING AREA",
    "TOTAL LIVING AREA",
    "ROOF",
    "EXTERIOR",
    "FOUNDATION",
    "BASEMENT",
    "FLOORING",
    "HEATING/COOLING",
    "FUEL SUPPLY",
    "DRINKING WATER",
    "SEWER",
    "HAS GARAGE",
    "PARKING",
    "WATERFRONT",
    "LAND FEATURES",
    "PROPERTY FEATURES",
    "UTILITIES",
    "APPLIANCES INCL.",
    "INCLUSIONS",
    "EXCLUSIONS",
    "LISTED BY",
}


@pytest.fixture(scope="module")
def html() -> str:
    return FIXTURE.read_text()


def test_bootstrap_carries_the_structured_record(html):
    bootstrap = parse_bootstrap(html)

    assert bootstrap["listing_id"] == "202603269"
    assert bootstrap["class_id"] == "1"
    assert bootstrap["status_id"] == "2"
    assert bootstrap["list_price"] == "499000"
    assert bootstrap["pix_count"] == "49"
    assert bootstrap["nbeds"] == "4"
    assert bootstrap["latitude"] == "46.10131396"
    assert bootstrap["longitude"] == "-60.74911817"


def test_bootstrap_is_empty_when_absent():
    assert parse_bootstrap("<html><body>no script here</body></html>") == {}
    assert parse_bootstrap("") == {}


def test_detail_items_use_the_uppercase_keys_the_clients_read(html):
    details = parse_detail_items(html)

    missing = REQUIRED_DETAIL_FIELDS - set(details)
    assert not missing, f"detail fields missing from the parse: {sorted(missing)}"

    assert details["BEDS"] == "4"
    assert details["BATHROOMS (F/H)"] == "2 / 0"
    assert details["ROOF"] == "Metal"
    assert details["BASEMENT"] == "Full, Partial, Crawl Space, Other"
    assert details["MAIN LIVING AREA"] == "2,300 sqft"


def test_detail_items_skip_unrendered_templates(html):
    """The page ships handlebars templates using the same class names."""
    details = parse_detail_items(html)
    assert not any("{{" in key or "{{" in value for key, value in details.items())


def test_detail_items_flatten_nested_markup(html):
    """Parcel sizes wrap their unit in an <abbr>, which must not be lost."""
    assert parse_detail_items(html)["PROV. PARCEL SIZE"] == "17,520 sqft"


def test_overlay_reads_the_hero_banner(html):
    overlay = parse_overlay(html)
    assert overlay["Address"] == "523 Chebucto Street, Baddeck"
    assert overlay["Status"] == "Sold"


def test_parse_cutsheet_returns_all_four_parts(html):
    parsed = parse_cutsheet(html)
    assert set(parsed) == {"bootstrap", "details", "overlay", "description"}
    assert parsed["bootstrap"] and parsed["details"] and parsed["overlay"]
    assert parsed["description"]


def test_parsers_tolerate_empty_input():
    assert parse_detail_items("") == {}
    assert parse_overlay("") == {}
    assert parse_description("") == ""
    assert parse_cutsheet("") == {
        "bootstrap": {},
        "details": {},
        "overlay": {},
        "description": "",
    }


def test_description_reads_the_full_span(html):
    description = parse_description(html)
    assert description.startswith("Own a true piece of Baddeck history")
    assert "Cabot Trail" in description
    assert "..." not in description.split("Manse")[0]


def test_description_falls_back_to_json_ld():
    html = """
    <html><body>
      <script type="application/ld+json">
        {"@type": "House", "description": "A quiet lot on the bay."}
      </script>
    </body></html>
    """
    assert parse_description(html) == "A quiet lot on the bay."


def test_description_skips_unrendered_templates():
    html = '<span class="full-description">{{info.description}}</span>'
    assert parse_description(html) == ""


def test_description_from_api_reads_the_cutsheet_payload():
    body = {
        "status": "success",
        "cutsheet": {"description": "Life is a beach!!  Two acres of frontage."},
    }
    assert description_from_api(body) == "Life is a beach!! Two acres of frontage."
    assert description_from_api({"description": "A cottage."}) == "A cottage."
    assert description_from_api({}) == ""
