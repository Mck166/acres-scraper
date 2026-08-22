"""Photo URL handling.

Viewpoint serves listing photos from two interchangeable, fully predictable URL
shapes:

    /property/cutimage/{photo_group_id}/{n}.jpg?sd=lg&cch={hash}
    /property/cutimagel/{listing_id}/{class_id}/{n}.jpg?&sd=summary&cch={hash}

Both return the same bytes. The second is what the database already stores and
is the one we emit, because it can be built from the listing id we key on.

Two behaviours drive the design here:

* The cutsheet page renders only the first four photos, so scraping the markup
  for image tags can never find the whole set. The real total is printed in the
  page as "49 photos", and every index from 1 to that total resolves.
* An out-of-range index does not 404. It returns a placeholder image with HTTP
  200, so a status code cannot tell us where the set ends. Detecting the
  placeholder needs a content comparison, which is what `PlaceholderProbe` does.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_SIZE = "summary"

# Matches both URL shapes. The group form has no class_id segment.
PHOTO_URL_RE = re.compile(
    r"/property/(?P<kind>cutimagel|cutimage)/(?P<first>\d+)/(?:(?P<class_id>\d+)/)?(?P<index>\d+)\.jpg"
    r"(?P<query>[^\"'\s>]*)",
    re.IGNORECASE,
)

CCH_RE = re.compile(r"cch=(?P<cch>[0-9a-zA-Z]+)", re.IGNORECASE)

# <span class="cutsheet-photo-indicator">49 photos</span>
PHOTO_INDICATOR_RE = re.compile(
    r"cutsheet-photo-indicator[^>]*>\s*([\d,]+)\s*photo", re.IGNORECASE
)

# An index beyond the end of a listing's set that viewpoint will still answer.
PROBE_INDEX = 9999


@dataclass(frozen=True)
class PhotoSet:
    """Everything needed to build a listing's photo URLs."""

    listing_id: str
    class_id: str
    count: int
    cch: str


def build_photo_url(
    listing_id: str,
    class_id: str,
    index: int,
    cch: str,
    base_url: str = "https://www.viewpoint.ca",
    size: str = DEFAULT_SIZE,
) -> str:
    return (
        f"{base_url.rstrip('/')}/property/cutimagel/{listing_id}/{class_id}/{index}.jpg"
        f"?&sd={size}&cch={cch}"
    )


def parse_photo_url(url: str) -> Optional[Dict[str, str]]:
    """Pull the identifiers out of either photo URL shape."""
    if not url:
        return None
    match = PHOTO_URL_RE.search(str(url))
    if not match:
        return None

    groups = match.groupdict()
    cch_match = CCH_RE.search(groups.get("query") or "")

    parsed = {
        "kind": groups["kind"].lower(),
        "index": groups["index"],
        "cch": cch_match.group("cch") if cch_match else "",
    }
    if parsed["kind"] == "cutimagel":
        parsed["listing_id"] = groups["first"]
        parsed["class_id"] = groups["class_id"] or "1"
    else:
        parsed["photo_group_id"] = groups["first"]
        parsed["class_id"] = "1"
    return parsed


def derive_photo_urls(
    listing_id: str,
    class_id: str,
    count: int,
    cch: str,
    base_url: str = "https://www.viewpoint.ca",
    size: str = DEFAULT_SIZE,
) -> List[str]:
    """Build the full ``1..count`` photo set for a listing."""
    if not listing_id or count <= 0:
        return []
    return [
        build_photo_url(listing_id, class_id, index, cch, base_url, size)
        for index in range(1, count + 1)
    ]


def photo_urls_for(photo_set: Optional[PhotoSet], base_url: str = "https://www.viewpoint.ca") -> List[str]:
    if not photo_set:
        return []
    return derive_photo_urls(
        photo_set.listing_id, photo_set.class_id, photo_set.count, photo_set.cch, base_url
    )


def extract_photo_count(html: str) -> Optional[int]:
    """Read the listing's own photo total out of the page."""
    if not html:
        return None
    match = PHOTO_INDICATOR_RE.search(str(html))
    if not match:
        return None
    count = int(match.group(1).replace(",", ""))
    return count or None


def extract_cache_hash(html: str) -> Optional[str]:
    """Find the per-listing cache-buster used on its photo URLs."""
    if not html:
        return None
    for match in PHOTO_URL_RE.finditer(str(html)):
        cch_match = CCH_RE.search(match.group("query") or "")
        if cch_match:
            return cch_match.group("cch")
    return None


def extract_photo_set(html: str, listing_id: str, class_id: str = "1") -> Optional[PhotoSet]:
    """Work out a listing's complete photo set from its cutsheet page."""
    if not listing_id:
        return None

    count = extract_photo_count(html)
    cch = extract_cache_hash(html) or ""

    if count is None:
        # No indicator: fall back to however many photos the page did render.
        indices = [
            int(match.group("index"))
            for match in PHOTO_URL_RE.finditer(str(html or ""))
        ]
        if not indices:
            return None
        count = max(indices)

    return PhotoSet(listing_id=str(listing_id), class_id=str(class_id), count=count, cch=cch)


def verify_photo_set(photo_set: PhotoSet, probe: "PlaceholderProbe") -> PhotoSet:
    """Trim a photo set to the photos the site will actually serve."""
    if photo_set is None:
        return photo_set

    actual = probe.verified_count(photo_set)
    if actual == photo_set.count:
        return photo_set

    return PhotoSet(
        listing_id=photo_set.listing_id,
        class_id=photo_set.class_id,
        count=actual,
        cch=photo_set.cch,
    )


def normalize_photo_list(photos: Optional[Sequence[Any]]) -> List[str]:
    """Clean, de-duplicate, and order a photo list."""
    if not photos:
        return []

    cleaned: List[str] = []
    seen = set()
    for photo in photos:
        if not photo:
            continue
        url = str(photo).strip().replace("&amp;", "&")
        if not url or url in seen:
            continue
        seen.add(url)
        cleaned.append(url)

    parsed = [parse_photo_url(url) for url in cleaned]
    if cleaned and all(parsed):
        order = sorted(range(len(cleaned)), key=lambda i: int(parsed[i]["index"]))
        cleaned = [cleaned[i] for i in order]

    return cleaned


class PlaceholderProbe:
    """Tells real photos apart from viewpoint's out-of-range placeholder.

    Viewpoint answers any index with HTTP 200, serving a stand-in image once a
    listing's photos run out, so a status code says nothing about whether a
    photo exists. A listing's advertised photo count is also occasionally one
    too high, which is what makes this check worth doing.

    The placeholder is learned per listing by requesting an index that cannot
    exist. Images are compared by hashing their leading bytes rather than the
    whole file, because viewpoint serves them without a Content-Length header
    and a full download per check would be wasteful.
    """

    SIGNATURE_BYTES = 8192

    def __init__(self, session, base_url: str = "https://www.viewpoint.ca"):
        self.session = session
        self.base_url = base_url
        self._placeholders: Dict[str, Optional[str]] = {}
        self._verdicts: Dict[str, bool] = {}

    def _signature(self, url: str) -> Optional[str]:
        """Hash the start of an image, enough to tell two images apart."""
        try:
            response = self.session.get(url, timeout=30, stream=True)
        except Exception:
            return None

        try:
            if response.status_code != 200:
                return None
            for chunk in response.iter_content(self.SIGNATURE_BYTES):
                return hashlib.md5(chunk).hexdigest()
            return None
        except Exception:
            return None
        finally:
            response.close()

    def _placeholder(self, photo_set: PhotoSet, size: str) -> Optional[str]:
        key = f"{photo_set.listing_id}/{photo_set.class_id}/{size}"
        if key not in self._placeholders:
            url = build_photo_url(
                photo_set.listing_id, photo_set.class_id, PROBE_INDEX, photo_set.cch, self.base_url, size
            )
            self._placeholders[key] = self._signature(url)
        return self._placeholders[key]

    def is_real_photo(self, photo_set: PhotoSet, index: int, size: str = DEFAULT_SIZE) -> bool:
        key = f"{photo_set.listing_id}/{photo_set.class_id}/{size}/{index}"
        if key in self._verdicts:
            return self._verdicts[key]

        placeholder = self._placeholder(photo_set, size)
        if placeholder is None:
            # Without a placeholder to compare against there is nothing to judge,
            # so trust the advertised count rather than discard photos.
            verdict = True
        else:
            url = build_photo_url(
                photo_set.listing_id, photo_set.class_id, index, photo_set.cch, self.base_url, size
            )
            signature = self._signature(url)
            verdict = signature is not None and signature != placeholder

        self._verdicts[key] = verdict
        return verdict

    def verified_count(self, photo_set: PhotoSet, size: str = DEFAULT_SIZE) -> int:
        """The number of photos a listing actually serves.

        Costs one request when the advertised count is right, and a binary
        search over the range when it overshoots.
        """
        if photo_set.count <= 0:
            return 0

        if self.is_real_photo(photo_set, photo_set.count, size):
            return photo_set.count

        # Photos are served as an unbroken run from index 1, so the boundary
        # between real and placeholder can be bisected.
        low, high = 0, photo_set.count
        while high - low > 1:
            middle = (low + high) // 2
            if self.is_real_photo(photo_set, middle, size):
                low = middle
            else:
                high = middle
        return low
