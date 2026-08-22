"""Try candidate parameter shapes against viewpoint's listing endpoints.

    python tools/try_params.py

The SPA builds identifiers as {pid, class_id} for properties and
{class_id, listing_id} for listings, but which endpoint wants which is not
obvious from the minified bundle. This just tries them and reports what sticks.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sanitize_fixtures import scrub  # noqa: E402

from scraper.client import ViewpointClient, ViewpointError  # noqa: E402
from scraper.config import get_settings  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "viewpoint"


def sample_listing():
    body = json.loads((FIXTURES / "newtoday.json").read_text())
    for listing in body.get("listings", []):
        if listing.get("pix_count") and int(listing["pix_count"]) > 4 and listing.get("pid"):
            return listing
    return body["listings"][0]


def main() -> int:
    listing = sample_listing()
    print("Sample listing:")
    for key in ("id", "listing_id", "class_id", "pid", "pix_count", "pix_cache_id", "address"):
        print(f"  {key} = {listing.get(key)}")

    candidates = [
        ("pid+class_id", {"pid": listing["pid"], "class_id": listing["class_id"]}),
        ("listing_id+class_id", {"listing_id": listing["listing_id"], "class_id": listing["class_id"]}),
        ("id+class_id", {"id": listing["id"], "class_id": listing["class_id"]}),
        ("listing_id only", {"listing_id": listing["listing_id"]}),
        ("pid only", {"pid": listing["pid"]}),
    ]

    with ViewpointClient(get_settings()) as client:
        for method in ("photos", "details", "history", "cutsheet"):
            print(f"\n=== listing/{method} ===")
            for label, params in candidates:
                try:
                    body = client.call("listing", method, params)
                except ViewpointError as exc:
                    print(f"  {label:24s} -> {str(exc)[:90]}")
                    continue

                keys = [k for k in body if k not in ("status", "nonce", "api_user", "api_login")]
                print(f"  {label:24s} -> OK keys={keys}")
                path = FIXTURES / f"{method}.json"
                path.write_text(json.dumps(scrub(body), indent=2, sort_keys=True, default=str))
                print(f"  {'':24s}    saved {path.name}")
                break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
