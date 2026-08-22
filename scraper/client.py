"""HTTP client for viewpoint's private JSON API.

The site's single-page app talks to ``/api/v2/{controller}/{method}``. Three
things are needed to join in, and all of them come off any authenticated page:

``CLIENT_VER``  a build number sent with every call
``NONCES``      a small pool of single-use tokens
``APIKEY``      an id and hash pair used to mint more nonces

Every successful response carries the next nonce, so after bootstrapping the
pool refills itself. Calls are serialized and rate limited; this is a small
scraper and there is nothing to gain from hammering.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests

from .config import Settings, get_settings
from .cutsheet import parse_cutsheet
from .identity import build_cutsheet_url, parse_cutsheet_url
from .normalize import as_since, format_price, parse_price, status_from_id
from .photos import (
    PhotoSet,
    PlaceholderProbe,
    extract_photo_set,
    normalize_photo_list,
    photo_urls_for,
    verify_photo_set,
)

log = logging.getLogger(__name__)

CLIENT_VER_RE = re.compile(r"CLIENT_VER\s*:\s*['\"]([^'\"]+)['\"]")
NONCES_RE = re.compile(r"NONCES\s*:\s*\[([^\]]*)\]")
APIKEY_RE = re.compile(r"APIKEY\s*:\s*\{\s*id\s*:\s*(\d+)\s*,\s*hash\s*:\s*['\"]([^'\"]+)['\"]", re.S)
TOKEN_RE = re.compile(r"['\"]([0-9a-fA-F]{16,})['\"]")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MAX_ATTEMPTS = 3


class ViewpointError(RuntimeError):
    """A call to viewpoint failed."""


class ViewpointAuthError(ViewpointError):
    """Login failed or the session is no longer valid."""


class ViewpointClient:
    """Talks to viewpoint over HTTP, no browser required."""

    def __init__(self, settings: Optional[Settings] = None, session: Optional[requests.Session] = None):
        self.settings = settings or get_settings()
        self.base_url = self.settings.viewpoint_base_url
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/map",
            }
        )

        self.client_ver: Optional[str] = None
        self.api_key_id: Optional[str] = None
        self.api_key_hash: Optional[str] = None
        self._nonces: List[str] = []
        self._last_request_at = 0.0
        self._logged_in = False
        self._probe: Optional[PlaceholderProbe] = None

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "ViewpointClient":
        self.login()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

    # -- bootstrap -------------------------------------------------------

    def bootstrap(self, path: str = "/map") -> None:
        """Read the API parameters the page hands to its own JavaScript."""
        response = self.session.get(f"{self.base_url}{path}", timeout=30)
        response.raise_for_status()
        html = response.text

        client_ver = CLIENT_VER_RE.search(html)
        if client_ver:
            self.client_ver = client_ver.group(1)

        api_key = APIKEY_RE.search(html)
        if api_key:
            self.api_key_id, self.api_key_hash = api_key.group(1), api_key.group(2)

        nonces = NONCES_RE.search(html)
        if nonces:
            self._nonces.extend(TOKEN_RE.findall(nonces.group(1)))

        if not self.client_ver:
            raise ViewpointError(f"Could not read CLIENT_VER from {path}")

        log.debug(
            "Bootstrapped: client_ver=%s, api_key_id=%s, %d nonces",
            self.client_ver,
            self.api_key_id,
            len(self._nonces),
        )

    def login(self) -> None:
        if self._logged_in:
            return
        if not self.settings.viewpoint_user or not self.settings.viewpoint_pass:
            raise ViewpointAuthError("VIEWPOINT_USER and VIEWPOINT_PASS must be set")

        self.bootstrap()

        payload = {
            "email": self.settings.viewpoint_user,
            "password": self.settings.viewpoint_pass,
            "remember": "1",
            "CLIENT_VER": self.client_ver,
        }
        response = self.session.post(
            f"{self.base_url}/api/v2/user/login",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=30,
        )

        try:
            body = response.json()
        except ValueError:
            raise ViewpointAuthError(f"Login returned non-JSON (HTTP {response.status_code})")

        if body.get("status") != "success":
            raise ViewpointAuthError(f"Login rejected: {body.get('errors') or body}")

        self._harvest_nonce(body)
        # The session now belongs to a signed-in user, so re-read the page to
        # pick up nonces minted for that user.
        self.bootstrap()
        self._logged_in = True
        log.info("Logged in to viewpoint as %s", self.settings.viewpoint_user)

    # -- nonces ----------------------------------------------------------

    def _harvest_nonce(self, body: Any) -> None:
        if isinstance(body, dict):
            nonce = body.get("nonce")
            if isinstance(nonce, str) and nonce:
                self._nonces.append(nonce)
            elif isinstance(nonce, list):
                self._nonces.extend(token for token in nonce if isinstance(token, str))

    def _mint_nonces(self) -> None:
        if not self.api_key_id or not self.api_key_hash:
            raise ViewpointError("No API key available to mint a nonce")

        response = self.session.post(
            f"{self.base_url}/api/v2/api/nonce",
            data={"kid": self.api_key_id, "hash": self.api_key_hash, "CLIENT_VER": self.client_ver},
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=30,
        )
        try:
            body = response.json()
        except ValueError:
            raise ViewpointError("Nonce request returned non-JSON")

        self._harvest_nonce(body)
        if not self._nonces:
            raise ViewpointError(f"Nonce request produced nothing: {body}")

    def _take_nonce(self) -> str:
        if not self._nonces:
            self._mint_nonces()
        return self._nonces.pop(0)

    # -- requests --------------------------------------------------------

    def _throttle(self) -> None:
        delay = self.settings.request_delay_seconds
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_at = time.monotonic()

    def call(
        self,
        controller: str,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        post: bool = False,
        needs_nonce: bool = True,
    ) -> Dict[str, Any]:
        """Make one API call, retrying transient failures."""
        url = f"{self.base_url}/api/v2/{controller}/{method}"
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            payload: Dict[str, Any] = dict(params or {})
            payload["CLIENT_VER"] = self.client_ver
            if needs_nonce:
                payload["nonce"] = self._take_nonce()

            self._throttle()
            try:
                if post:
                    response = self.session.post(
                        url,
                        data=payload,
                        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                        timeout=30,
                    )
                else:
                    response = self.session.get(url, params=payload, timeout=30)
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2 ** attempt)
                continue

            try:
                body = response.json()
            except ValueError:
                last_error = ViewpointError(f"{controller}/{method} returned non-JSON (HTTP {response.status_code})")
                time.sleep(2 ** attempt)
                continue

            self._harvest_nonce(body)

            if body.get("status") == "success":
                return body

            errors = body.get("errors") or body.get("error")
            last_error = ViewpointError(f"{controller}/{method} failed: {errors}")

            # A stale or spent nonce is the common failure; a fresh one usually fixes it.
            if needs_nonce:
                self._nonces.clear()
            time.sleep(1)

        raise last_error or ViewpointError(f"{controller}/{method} failed")

    # -- endpoints -------------------------------------------------------

    def new_today(self, since: Any = None) -> Dict[str, Any]:
        """The activity feed, covering everything changed since a given moment.

        Sending an empty ``since`` returns a fixed window that lags behind and
        omits changes made today, so a timestamp is always sent.
        """
        return self.call("listing", "newtoday", {"since": as_since(since)})

    def listing_photos(self, listing_id: str, class_id: str = "1") -> Dict[str, Any]:
        return self.call("listing", "photos", {"listing_id": listing_id, "class_id": class_id})

    def listing_cutsheet(self, listing_id: str, class_id: str = "1") -> Dict[str, Any]:
        """The listing's structured record, the same one the page embeds."""
        return self.call("listing", "cutsheet", {"listing_id": listing_id, "class_id": class_id})

    def fetch_page(self, url: str) -> str:
        """Fetch a rendered page, for the data that only exists in markup."""
        self._throttle()
        response = self.session.get(url, headers={"Accept": "text/html"}, timeout=30)
        response.raise_for_status()
        return response.text

    # -- transport interface --------------------------------------------
    # Mirrors SeleniumClient so the sync engine does not care which is in use.

    def new_today_activity(self, since: Any = None) -> List[Dict[str, Any]]:
        """The changed listings, one entry per listing.

        A single call returns every listing that changed along with its price,
        status, coordinates, and photo count, so nothing further is needed to
        decide what is worth fetching in full.
        """
        body = self.new_today(since=since)
        listings = body.get("listings") or []

        events_by_listing: Dict[str, List[Dict[str, Any]]] = {}
        for event in body.get("events") or []:
            listing_id = str(event.get("listing_id") or "")
            if listing_id:
                events_by_listing.setdefault(listing_id, []).append(event)

        activity = []
        for listing in listings:
            listing_id = str(listing.get("listing_id") or "")
            if not listing_id:
                continue
            entry = dict(listing)
            entry["events"] = events_by_listing.get(listing_id, [])
            entry["url"] = build_cutsheet_url(
                listing_id, str(listing.get("class_id") or "1"), self.base_url
            )
            activity.append(entry)

        log.info(
            "new-today: %d listings, %d change events",
            len(activity),
            len(body.get("events") or []),
        )
        return activity

    def new_today_urls(self, since: Any = None) -> List[str]:
        return [entry["url"] for entry in self.new_today_activity(since=since)]

    def fetch_listing(self, cutsheet_url: str) -> Optional[Dict[str, Any]]:
        """Scrape one listing into the raw shape the normalizer expects."""
        parsed = parse_cutsheet_url(cutsheet_url)
        if not parsed:
            log.warning("Not a cutsheet URL: %s", cutsheet_url)
            return None

        listing_id, class_id = parsed

        try:
            html = self.fetch_page(cutsheet_url)
        except requests.RequestException as exc:
            log.error("Could not fetch %s: %s", cutsheet_url, exc)
            return None

        parts = parse_cutsheet(html)
        bootstrap = parts["bootstrap"]
        raw: Dict[str, Any] = dict(parts["details"])
        raw["url"] = cutsheet_url

        overlay = parts["overlay"]
        raw["Address"] = self._address_of(bootstrap) or overlay.get("Address", "")

        status = status_from_id(bootstrap.get("status_id"))
        raw["Status"] = status or overlay.get("Status", "")
        if bootstrap.get("status_id") is not None:
            raw["status_id"] = str(bootstrap["status_id"])

        # The stored price has always been the asking price, including for sold
        # listings; the sale figure is kept separately.
        raw["Price"] = format_price(bootstrap.get("list_price")) or overlay.get("Price", "")
        sold_price = parse_price(bootstrap.get("sold_price"))
        if sold_price:
            raw["sold_price"] = sold_price

        raw["latitude"] = bootstrap.get("latitude")
        raw["longitude"] = bootstrap.get("longitude")
        if bootstrap.get("pid"):
            raw["PID"] = str(bootstrap["pid"])

        for source, target in (
            ("list_dt", "listed_on"),
            ("sold_dt", "sold_on"),
            ("close_dt", "closes_on"),
            ("update_dt", "source_updated_at"),
        ):
            if bootstrap.get(source):
                raw[target] = bootstrap[source]

        raw["Photos"] = self._photos_for(html, bootstrap, listing_id, class_id)
        raw["Photo_Count"] = len(raw["Photos"])

        return raw

    @staticmethod
    def _address_of(bootstrap: Dict[str, Any]) -> str:
        address = str(bootstrap.get("address") or "").strip()
        city = str(bootstrap.get("city") or "").strip()
        if address and city and not address.endswith(city):
            return f"{address}, {city}"
        return address

    def _photos_for(
        self, html: str, bootstrap: Dict[str, Any], listing_id: str, class_id: str
    ) -> List[str]:
        """Build the photo set, preferring the count the page states outright."""
        photo_set = extract_photo_set(html, listing_id, class_id)

        count = bootstrap.get("pix_count")
        if count is not None:
            try:
                stated = int(count)
            except (TypeError, ValueError):
                stated = 0
            if stated > 0:
                cch = photo_set.cch if photo_set else str(bootstrap.get("pix_cache_id") or "")
                photo_set = PhotoSet(
                    listing_id=listing_id, class_id=class_id, count=stated, cch=cch
                )

        if photo_set is not None:
            # The advertised count is occasionally one higher than the number of
            # photos actually served, and an over-long set shows the app a
            # placeholder image as though it were a real photo.
            photo_set = verify_photo_set(photo_set, self._photo_probe())

        return normalize_photo_list(photo_urls_for(photo_set, self.base_url))

    def _photo_probe(self) -> PlaceholderProbe:
        if self._probe is None:
            self._probe = PlaceholderProbe(self.session, self.base_url)
        return self._probe
