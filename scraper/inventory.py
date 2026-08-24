"""Discover and enumerate the whole Nova Scotia for-sale market.

Viewpoint's daily scraper only sees ``listing/newtoday`` plus listings already
in Mongo. A gap-fill needs an enumerator that returns every active listing id.
This module probes candidate JSON endpoints, captures the authenticated map
SPA's network calls, and ranks whatever actually returns inventory.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse

from .client import ViewpointClient, ViewpointError
from .identity import build_cutsheet_url
from .normalize import date_is_today

log = logging.getLogger(__name__)

ACTIVE_STATUS_IDS = frozenset({"5", "6", "7"})
EVENT_DATE_FIELDS = ("listed_on", "price_changed_on", "pending_on", "sold_on")

API_PATH_RE = re.compile(r"/api/v2/([^/?]+)/([^/?]+)")
CUTSHEET_RE = re.compile(r"/cutsheet/(\d+)(?:/(\d+))?", re.IGNORECASE)

# Nova Scotia, with a little water around the coast.
NS_BBOX = (43.3, -66.5, 47.2, -59.5)  # sw_lat, sw_lng, ne_lat, ne_lng
NS_SEARCH_AREA = "45.2, -63.1, 7, 43.3, -66.5, 47.2, -59.5"
MIN_TILE_SPAN = 0.05
MAX_SEARCH_TILES = 800

OUTPUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "output" / "debug"
PROBE_RESULT_PATH = OUTPUT_DIR / "inventory_probe.json"

# Ten years: everything that has ever changed, not never-touched listings.
OLD_SINCE = datetime(2016, 1, 1, tzinfo=timezone.utc)

SEARCH_MAP_NS = {
    "parameters[search_type]": "property",
    "parameters[search_area_type]": "map",
    "parameters[search_area]": NS_SEARCH_AREA,
}
SEARCH_PROVINCE_NS = {
    "parameters[search_type]": "property",
    "parameters[search_area_type]": "province",
    "parameters[search_area]": "Nova Scotia",
}

# listing/list, listing/map, listing/results 404. listing/search needs the
# SPA's parameters[search_*] keys; without them it returns code 17. A
# province-wide search returns code 402 (too many), which is the signal to
# tile listing/search across NS_BBOX.
API_CANDIDATES: List[tuple] = [
    ("listing", "search", SEARCH_MAP_NS, False),
    ("listing", "search", SEARCH_PROVINCE_NS, False),
    ("listing", "search", {}, False),
    ("listing", "vp", {}, False),
    ("listing", "preferred", {**SEARCH_MAP_NS, "n": "0"}, False),
]


def has_atlantic_today_event(document: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True when any MLS event date is calendar-today in America/Halifax."""
    return any(date_is_today(document.get(field), now) for field in EVENT_DATE_FIELDS)


def listing_record(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    listing_id = str(node.get("listing_id") or "")
    if not listing_id.isdigit() or len(listing_id) < 8:
        return None
    return {
        "listing_id": listing_id,
        "class_id": str(node.get("class_id") or "1"),
        "status_id": str(node.get("status_id") or ""),
        "list_price": node.get("list_price"),
        "list_dt": node.get("list_dt"),
        "status_dt": node.get("status_dt"),
        "latitude": node.get("latitude"),
        "longitude": node.get("longitude"),
        "address": node.get("address"),
        "pid": node.get("pid"),
    }


def extract_listings(body: Any) -> List[Dict[str, Any]]:
    """Walk an arbitrary JSON payload and collect listing-shaped dicts."""
    found: Dict[str, Dict[str, Any]] = {}
    stack: List[Any] = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            record = listing_record(node)
            if record:
                found[record["listing_id"]] = record
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return list(found.values())


def extract_cutsheet_refs(text: str) -> List[Dict[str, str]]:
    found: Dict[str, Dict[str, str]] = {}
    for listing_id, class_id in CUTSHEET_RE.findall(text or ""):
        found[listing_id] = {
            "listing_id": listing_id,
            "class_id": class_id or "1",
            "status_id": "",
        }
    return list(found.values())


def parse_api_request(url: str) -> Optional[Dict[str, Any]]:
    """Controller/method/params from a captured Viewpoint API URL."""
    match = API_PATH_RE.search(url or "")
    if not match:
        return None
    parsed = urlparse(url)
    params: Dict[str, Any] = {}
    for key, values in parse_qs(parsed.query).items():
        if key.lower() in ("nonce", "client_ver"):
            continue
        params[key] = values[0] if len(values) == 1 else values
    return {
        "controller": match.group(1),
        "method": match.group(2),
        "params": params,
        "post": False,
    }


def is_active_listing(entry: Dict[str, Any]) -> bool:
    status_id = str(entry.get("status_id") or "")
    if not status_id:
        return True
    return status_id in ACTIVE_STATUS_IDS


def filter_active(listings: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in listings if is_active_listing(item)]


def is_too_many_results(error: Any) -> bool:
    text = error.get("error") if isinstance(error, dict) else error
    return "Too many search results" in str(text or "")


def map_search_params(
    sw_lat: float,
    sw_lng: float,
    ne_lat: float,
    ne_lng: float,
    zoom: Optional[int] = None,
) -> Dict[str, str]:
    center_lat = (sw_lat + ne_lat) / 2
    center_lng = (sw_lng + ne_lng) / 2
    span = max(abs(ne_lat - sw_lat), abs(ne_lng - sw_lng))
    if zoom is None:
        if span < 0.15:
            zoom = 12
        elif span < 0.4:
            zoom = 10
        elif span < 1.5:
            zoom = 8
        else:
            zoom = 7
    area = f"{center_lat}, {center_lng}, {zoom}, {sw_lat}, {sw_lng}, {ne_lat}, {ne_lng}"
    return {
        "parameters[search_type]": "property",
        "parameters[search_area_type]": "map",
        "parameters[search_area]": area,
    }


def split_bbox(sw_lat: float, sw_lng: float, ne_lat: float, ne_lng: float):
    mid_lat = (sw_lat + ne_lat) / 2
    mid_lng = (sw_lng + ne_lng) / 2
    return [
        (sw_lat, sw_lng, mid_lat, mid_lng),
        (sw_lat, mid_lng, mid_lat, ne_lng),
        (mid_lat, sw_lng, ne_lat, mid_lng),
        (mid_lat, mid_lng, ne_lat, ne_lng),
    ]


def search_map_tile(client: ViewpointClient, bbox: tuple) -> Optional[List[Dict[str, Any]]]:
    """Return listings in a map tile, or None when Viewpoint refuses the area as too large."""
    params = map_search_params(*bbox)
    try:
        body = client.call("listing", "search", params, max_attempts=1)
    except ViewpointError as exc:
        if is_too_many_results(exc):
            return None
        raise
    return extract_listings(body)


def enumerate_search_tiles(
    client: ViewpointClient,
    bbox: tuple = NS_BBOX,
    min_span: float = MIN_TILE_SPAN,
    max_tiles: int = MAX_SEARCH_TILES,
) -> List[Dict[str, Any]]:
    """Walk NS as a quadtree of listing/search tiles until every cell fits."""
    found: Dict[str, Dict[str, Any]] = {}
    tiles = 0
    queue: List[tuple] = [bbox]
    while queue:
        tile = queue.pop()
        tiles += 1
        if tiles > max_tiles:
            log.warning("Stopped tiling after %s tiles; %s listing ids so far", max_tiles, len(found))
            break
        sw_lat, sw_lng, ne_lat, ne_lng = tile
        span = max(abs(ne_lat - sw_lat), abs(ne_lng - sw_lng))
        listings = search_map_tile(client, tile)
        if listings is None:
            if span <= min_span:
                log.warning("Tile still over the result cap at min span %s: %s", span, tile)
                continue
            queue.extend(split_bbox(*tile))
            continue
        for item in listings:
            found[item["listing_id"]] = item
        if listings:
            log.info("Tile %s -> %s listings (%s unique)", tile, len(listings), len(found))
    log.info("Tiled listing/search: %s tiles, %s unique listings", tiles, len(found))
    return list(found.values())


def _attempt_label(controller: str, method: str, params: Dict[str, Any], post: bool) -> str:
    verb = "POST" if post else "GET"
    return f"{verb} {controller}/{method} {params or {}}"


def try_api_call(
    client: ViewpointClient,
    controller: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    post: bool = False,
) -> Dict[str, Any]:
    params = params or {}
    label = _attempt_label(controller, method, params, post)
    try:
        body = client.call(controller, method, params, post=post)
    except ViewpointError as exc:
        return {
            "ok": False,
            "label": label,
            "controller": controller,
            "method": method,
            "params": params,
            "post": post,
            "error": str(exc)[:240],
            "listing_count": 0,
            "active_count": 0,
        }

    listings = extract_listings(body)
    active = filter_active(listings)
    keys = [key for key in body.keys() if key not in ("status", "nonce", "api_user", "api_login")]
    return {
        "ok": True,
        "label": label,
        "controller": controller,
        "method": method,
        "params": params,
        "post": post,
        "keys": keys,
        "listing_count": len(listings),
        "active_count": len(active),
        "sample": listings[:5],
    }


def capture_map_network(settings) -> Dict[str, Any]:
    """Load the authenticated /map SPA and collect its API + cutsheet traffic."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    from .selenium_client import SeleniumClient

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    api_calls: Dict[str, Dict[str, Any]] = {}
    cutsheets: Dict[str, Dict[str, str]] = {}
    page_cutsheets: List[Dict[str, str]] = []

    try:
        client = SeleniumClient(settings, driver=driver)
        client.login()
        map_url = f"{settings.viewpoint_base_url.rstrip('/')}/map"
        log.info("Loading %s", map_url)
        driver.get(map_url)
        time.sleep(8)

        page_cutsheets = extract_cutsheet_refs(driver.page_source)

        for entry in driver.get_log("performance"):
            try:
                message = json.loads(entry["message"])["message"]
            except (KeyError, ValueError, TypeError):
                continue
            if message.get("method") != "Network.requestWillBeSent":
                continue
            url = ((message.get("params") or {}).get("request") or {}).get("url") or ""
            parsed = parse_api_request(url)
            if parsed:
                key = f"{parsed['controller']}/{parsed['method']}"
                api_calls.setdefault(key, parsed)
            for ref in extract_cutsheet_refs(url):
                cutsheets[ref["listing_id"]] = ref
    finally:
        driver.quit()

    return {
        "api_calls": list(api_calls.values()),
        "cutsheet_from_network": list(cutsheets.values()),
        "cutsheet_from_page": page_cutsheets,
    }


def _score(attempt: Dict[str, Any]) -> tuple:
    """Prefer a true inventory dump: many actives with list_dt attached."""
    if not attempt.get("ok"):
        return (0, 0, 0)
    active = int(attempt.get("active_count") or 0)
    total = int(attempt.get("listing_count") or 0)
    sample = attempt.get("sample") or []
    with_dates = sum(1 for item in sample if item.get("list_dt"))
    return (active, total, with_dates)


def choose_enumerator(attempts: List[Dict[str, Any]], map_capture: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pick the first path that can enumerate the market. Never guess."""
    too_many = next((attempt for attempt in attempts if is_too_many_results(attempt)), None)
    if too_many:
        return {
            "kind": "search_tiles",
            "controller": "listing",
            "method": "search",
            "bbox": list(NS_BBOX),
            "reason": (
                "listing/search returns the market but refuses a province-wide "
                "query (code 402); tile the NS map bounds instead"
            ),
        }

    ranked = sorted(
        [attempt for attempt in attempts if attempt.get("method") != "newtoday"],
        key=_score,
        reverse=True,
    )
    best = ranked[0] if ranked else None

    if best and best.get("active_count", 0) >= 200:
        return {
            "kind": "api",
            "controller": best["controller"],
            "method": best["method"],
            "params": best.get("params") or {},
            "post": bool(best.get("post")),
            "listing_count": best.get("listing_count"),
            "active_count": best.get("active_count"),
            "reason": f"{best['label']} returned {best['active_count']} active listings",
        }

    newtoday_attempts = [
        attempt for attempt in attempts if attempt.get("method") == "newtoday" and attempt.get("ok")
    ]
    newtoday_default = next(
        (
            attempt
            for attempt in newtoday_attempts
            if str(attempt.get("label") or "").startswith("GET listing/newtoday default")
        ),
        None,
    )
    newtoday_old = max(
        newtoday_attempts,
        key=lambda attempt: int(attempt.get("listing_count") or 0),
        default=None,
    )
    old_count = (newtoday_old or {}).get("listing_count") or 0
    default_count = (newtoday_default or {}).get("listing_count") or 0
    if (
        newtoday_old is not None
        and newtoday_old is not newtoday_default
        and old_count > max(default_count, 0)
        and old_count >= 50
    ):
        return {
            "kind": "newtoday",
            "since": (newtoday_old.get("params") or {}).get("since") or str(int(OLD_SINCE.timestamp())),
            "listing_count": old_count,
            "active_count": newtoday_old.get("active_count"),
            "reason": (
                f"listing/newtoday since={newtoday_old.get('params')} returned "
                f"{old_count} listings (default window had {default_count})"
            ),
        }

    map_ids = 0
    if map_capture:
        seen = {
            item["listing_id"]
            for group in (
                map_capture.get("cutsheet_from_network") or [],
                map_capture.get("cutsheet_from_page") or [],
            )
            for item in group
            if item.get("listing_id")
        }
        map_ids = len(seen)
        if map_ids:
            return {
                "kind": "selenium_map",
                "listing_count": map_ids,
                "active_count": map_ids,
                "reason": f"map page exposed {map_ids} cutsheet URLs; no list-all API",
            }

    if best and best.get("ok") and best.get("listing_count", 0) > 0:
        return {
            "kind": "api",
            "controller": best["controller"],
            "method": best["method"],
            "params": best.get("params") or {},
            "post": bool(best.get("post")),
            "listing_count": best.get("listing_count"),
            "active_count": best.get("active_count"),
            "reason": f"best available API was {best['label']} ({best.get('listing_count')} listings)",
        }

    return {
        "kind": "newtoday",
        "since": str(int(OLD_SINCE.timestamp())),
        "reason": "no list-all API; falling back to newtoday with a multi-year since",
        "listing_count": old_count,
        "active_count": (newtoday_old or {}).get("active_count") or 0,
    }


def probe(client: ViewpointClient, capture_map: bool = True) -> Dict[str, Any]:
    """Try candidate endpoints and the map SPA; write a result file."""
    attempts: List[Dict[str, Any]] = []

    log.info("Probing listing/newtoday (default window)")
    default_since = datetime.now(timezone.utc) - timedelta(hours=24)
    default_attempt = try_api_call(
        client, "listing", "newtoday", {"since": str(int(default_since.timestamp()))}
    )
    default_attempt["label"] = "GET listing/newtoday default"
    attempts.append(default_attempt)

    log.info("Probing listing/newtoday (since %s)", OLD_SINCE.date())
    old_attempt = try_api_call(
        client, "listing", "newtoday", {"since": str(int(OLD_SINCE.timestamp()))}
    )
    old_attempt["label"] = f"GET listing/newtoday since={OLD_SINCE.date()}"
    attempts.append(old_attempt)

    for controller, method, params, post in API_CANDIDATES:
        log.info("Probing %s", _attempt_label(controller, method, params, post))
        attempts.append(try_api_call(client, controller, method, params, post=post))

    map_capture: Optional[Dict[str, Any]] = None
    if capture_map:
        try:
            log.info("Capturing authenticated /map network traffic")
            map_capture = capture_map_network(client.settings)
            for call in map_capture.get("api_calls") or []:
                already = any(
                    attempt.get("controller") == call["controller"]
                    and attempt.get("method") == call["method"]
                    and attempt.get("params") == call.get("params")
                    for attempt in attempts
                )
                if already:
                    continue
                log.info(
                    "Replaying map SPA call %s/%s %s",
                    call["controller"],
                    call["method"],
                    call.get("params"),
                )
                attempts.append(
                    try_api_call(
                        client,
                        call["controller"],
                        call["method"],
                        call.get("params") or {},
                        post=bool(call.get("post")),
                    )
                )
        except Exception as exc:
            log.warning("Map network capture failed: %s", exc)
            map_capture = {"error": str(exc), "api_calls": [], "cutsheet_from_network": [], "cutsheet_from_page": []}

    chosen = choose_enumerator(attempts, map_capture)
    result = {
        "chosen": chosen,
        "attempts": [
            {key: value for key, value in attempt.items() if key != "sample"}
            for attempt in attempts
        ],
        "map_capture": {
            "api_calls": (map_capture or {}).get("api_calls") or [],
            "cutsheet_network_count": len((map_capture or {}).get("cutsheet_from_network") or []),
            "cutsheet_page_count": len((map_capture or {}).get("cutsheet_from_page") or []),
            "error": (map_capture or {}).get("error"),
        },
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROBE_RESULT_PATH.write_text(json.dumps(result, indent=2, default=str))
    log.info("Wrote %s", PROBE_RESULT_PATH)
    log.info("Chosen enumerator: %s", chosen)
    return result


def load_probe_result(path: Optional[pathlib.Path] = None) -> Optional[Dict[str, Any]]:
    target = path or PROBE_RESULT_PATH
    if not target.exists():
        return None
    return json.loads(target.read_text())


def _listings_from_map(settings) -> List[Dict[str, Any]]:
    capture = capture_map_network(settings)
    found: Dict[str, Dict[str, Any]] = {}
    for group in (capture.get("cutsheet_from_network") or [], capture.get("cutsheet_from_page") or []):
        for item in group:
            found[item["listing_id"]] = item
    return list(found.values())


def enumerate_market(
    client: ViewpointClient,
    probe_result: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return active NS listings using the enumerator the probe selected."""
    result = probe_result or load_probe_result()
    if result is None:
        raise RuntimeError("Run tools.probe_inventory first; no enumerator has been chosen")

    chosen = result.get("chosen") or {}
    kind = chosen.get("kind")
    listings: List[Dict[str, Any]] = []

    if kind == "api":
        body = client.call(
            chosen["controller"],
            chosen["method"],
            chosen.get("params") or {},
            post=bool(chosen.get("post")),
        )
        listings = extract_listings(body)
    elif kind == "search_tiles":
        bbox = tuple(chosen.get("bbox") or NS_BBOX)
        listings = enumerate_search_tiles(client, bbox=bbox)
    elif kind == "newtoday":
        since = chosen.get("since") or str(int(OLD_SINCE.timestamp()))
        listings = extract_listings(client.new_today(since=since))
    elif kind == "selenium_map":
        listings = _listings_from_map(client.settings)
    else:
        raise RuntimeError(f"Unknown enumerator kind {kind!r}")

    active = filter_active(listings)
    log.info(
        "Enumerator %s: %d listings, %d active (status 5/6/7)",
        kind,
        len(listings),
        len(active),
    )
    return active


def cutsheet_url_for(entry: Dict[str, Any], base_url: str) -> str:
    return build_cutsheet_url(entry["listing_id"], entry.get("class_id") or "1", base_url)
