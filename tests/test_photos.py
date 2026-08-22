"""Photo URL derivation.

The fixture is a real cutsheet page for MLS 202603269, a listing with 49 photos
of which the page renders only four. It is the exact case the old click-through
extraction failed on.
"""

import pathlib

import pytest

from scraper.photos import (
    PhotoSet,
    build_photo_url,
    derive_photo_urls,
    extract_cache_hash,
    extract_photo_count,
    extract_photo_set,
    normalize_photo_list,
    parse_photo_url,
    photo_urls_for,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "pages" / "cutsheet_202603269_49photos.html"


@pytest.fixture(scope="module")
def cutsheet_html() -> str:
    return FIXTURE.read_text()


def test_page_advertises_more_photos_than_it_renders(cutsheet_html):
    """The premise of the fix: markup alone undercounts badly."""
    rendered = cutsheet_html.count("/property/cutimage/11673773/")
    assert extract_photo_count(cutsheet_html) == 49
    assert rendered < 49


def test_extract_cache_hash(cutsheet_html):
    assert extract_cache_hash(cutsheet_html) == "51edc21e"


def test_extract_photo_set_builds_the_whole_run(cutsheet_html):
    photo_set = extract_photo_set(cutsheet_html, "202603269", "1")

    assert photo_set == PhotoSet(listing_id="202603269", sequence="1", count=49, cch="51edc21e")

    urls = photo_urls_for(photo_set)
    assert len(urls) == 49
    assert urls[0].endswith("/property/cutimagel/202603269/1/1.jpg?&sd=summary&cch=51edc21e")
    assert urls[-1].endswith("/property/cutimagel/202603269/1/49.jpg?&sd=summary&cch=51edc21e")
    assert len(set(urls)) == 49


def test_extract_photo_set_returns_none_without_photos():
    assert extract_photo_set("<html><body>nothing here</body></html>", "202603269") is None


def test_extract_photo_set_falls_back_to_rendered_images():
    html = (
        '<img src="https://www.viewpoint.ca/property/cutimage/999/1.jpg?sd=lg&cch=abc123">'
        '<img src="https://www.viewpoint.ca/property/cutimage/999/2.jpg?sd=lg&cch=abc123">'
    )
    photo_set = extract_photo_set(html, "202600001", "1")
    assert photo_set.count == 2
    assert photo_set.cch == "abc123"


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://www.viewpoint.ca/property/cutimagel/202509640/1/7.jpg?&sd=summary&cch=1dd45d97",
            {"kind": "cutimagel", "listing_id": "202509640", "sequence": "1", "index": "7", "cch": "1dd45d97"},
        ),
        (
            "https://www.viewpoint.ca/property/cutimage/11673773/12.jpg?sd=lg&cch=51edc21e",
            {"kind": "cutimage", "photo_group_id": "11673773", "sequence": "1", "index": "12", "cch": "51edc21e"},
        ),
    ],
)
def test_parse_photo_url_handles_both_shapes(url, expected):
    assert parse_photo_url(url) == expected


def test_parse_photo_url_rejects_other_images():
    assert parse_photo_url("https://www.viewpoint.ca/assets/dist/images/logo/vp-logo.png") is None
    assert parse_photo_url("") is None


def test_derive_photo_urls_is_one_based_and_bounded():
    urls = derive_photo_urls("202509640", "1", 3, "abc")
    assert [parse_photo_url(url)["index"] for url in urls] == ["1", "2", "3"]
    assert derive_photo_urls("202509640", "1", 0, "abc") == []
    assert derive_photo_urls("", "1", 5, "abc") == []


def test_normalize_photo_list_dedupes_and_orders():
    photos = [
        build_photo_url("202509640", "1", 3, "abc"),
        build_photo_url("202509640", "1", 1, "abc"),
        build_photo_url("202509640", "1", 3, "abc"),
        build_photo_url("202509640", "1", 2, "abc"),
    ]
    ordered = normalize_photo_list(photos)
    assert [parse_photo_url(url)["index"] for url in ordered] == ["1", "2", "3"]


def test_normalize_photo_list_decodes_escaped_ampersands():
    escaped = "https://www.viewpoint.ca/property/cutimage/11673773/1.jpg?sd=lg&amp;cch=51edc21e"
    assert normalize_photo_list([escaped]) == [escaped.replace("&amp;", "&")]


def test_normalize_photo_list_handles_empties():
    assert normalize_photo_list(None) == []
    assert normalize_photo_list([None, "", "  "]) == []


def test_stored_production_urls_are_still_parseable(golden_docs):
    """Whatever we emit has to remain compatible with what is already stored."""
    for name, doc in golden_docs.items():
        for url in doc.get("Photos") or []:
            assert parse_photo_url(url), f"{name}: unparseable stored photo URL {url}"
