"""Photo URL derivation.

The fixture is a real cutsheet page for MLS 202603269, a listing with 49 photos
of which the page renders only four. It is the exact case the old click-through
extraction failed on.
"""

import pathlib

import pytest

from scraper.photos import (
    PhotoSet,
    PlaceholderProbe,
    build_photo_url,
    derive_photo_urls,
    extract_cache_hash,
    extract_photo_count,
    extract_photo_set,
    normalize_photo_list,
    parse_photo_url,
    photo_urls_for,
    verify_photo_set,
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

    assert photo_set == PhotoSet(listing_id="202603269", class_id="1", count=49, cch="51edc21e")

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
            {"kind": "cutimagel", "listing_id": "202509640", "class_id": "1", "index": "7", "cch": "1dd45d97"},
        ),
        (
            "https://www.viewpoint.ca/property/cutimage/11673773/12.jpg?sd=lg&cch=51edc21e",
            {"kind": "cutimage", "photo_group_id": "11673773", "class_id": "1", "index": "12", "cch": "51edc21e"},
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


class FakeImageSession:
    """Serves a run of distinct images and a placeholder past the end."""

    PLACEHOLDER = b"placeholder-bytes"

    def __init__(self, real_photos: int):
        self.real_photos = real_photos
        self.requests = 0

    def get(self, url, timeout=None, stream=False):
        self.requests += 1
        index = int(parse_photo_url(url)["index"])
        content = self.PLACEHOLDER if index > self.real_photos else f"photo-{index}".encode()
        return FakeImageResponse(content)


class FakeImageResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.status_code = 200
        self.headers = {}

    def iter_content(self, size):
        yield self.content

    def close(self):
        pass


def test_probe_accepts_a_correct_count():
    session = FakeImageSession(real_photos=12)
    probe = PlaceholderProbe(session)
    photo_set = PhotoSet(listing_id="202600001", class_id="1", count=12, cch="abc")

    assert probe.verified_count(photo_set) == 12


def test_probe_trims_an_overstated_count():
    """Listings occasionally advertise one more photo than they serve."""
    session = FakeImageSession(real_photos=50)
    probe = PlaceholderProbe(session)
    photo_set = PhotoSet(listing_id="202617893", class_id="1", count=51, cch="58cc582b")

    assert probe.verified_count(photo_set) == 50


def test_probe_bisects_rather_than_walking_back():
    session = FakeImageSession(real_photos=3)
    probe = PlaceholderProbe(session)
    photo_set = PhotoSet(listing_id="202600001", class_id="1", count=100, cch="abc")

    assert probe.verified_count(photo_set) == 3
    assert session.requests < 20, "the search should bisect, not scan"


def test_verify_photo_set_trims_to_the_real_count():
    session = FakeImageSession(real_photos=7)
    probe = PlaceholderProbe(session)
    photo_set = PhotoSet(listing_id="202600001", class_id="1", count=9, cch="abc")

    verified = verify_photo_set(photo_set, probe)
    assert verified.count == 7
    assert verified.listing_id == photo_set.listing_id
    assert verified.cch == photo_set.cch


def test_probe_trusts_the_count_when_it_cannot_reach_the_site():
    """A network failure must not silently empty a listing's photos."""

    class DeadSession:
        def get(self, *args, **kwargs):
            raise OSError("network down")

    photo_set = PhotoSet(listing_id="202600001", class_id="1", count=20, cch="abc")
    assert PlaceholderProbe(DeadSession()).verified_count(photo_set) == 20


def test_stored_production_urls_are_still_parseable(golden_docs):
    """Whatever we emit has to remain compatible with what is already stored."""
    for name, doc in golden_docs.items():
        for url in doc.get("Photos") or []:
            assert parse_photo_url(url), f"{name}: unparseable stored photo URL {url}"
