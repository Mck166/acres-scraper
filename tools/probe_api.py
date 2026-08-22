"""Probe viewpoint's JSON API and save the responses as test fixtures.

    python tools/probe_api.py [listing_id]

Responses are written to tests/fixtures/viewpoint/ so the client can be tested
offline. Credentials are never written: only responses are saved, and any value
that looks like the configured password is redacted before saving.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from scraper.client import ViewpointClient, ViewpointError  # noqa: E402
from scraper.config import get_settings  # noqa: E402

OUTPUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "viewpoint"


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret and secret in value:
                value = value.replace(secret, "REDACTED")
        return value
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    return value


def save(name, body, secrets):
    from sanitize_fixtures import scrub

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{name}.json"
    path.write_text(json.dumps(scrub(redact(body, secrets)), indent=2, sort_keys=True, default=str))
    print(f"  saved {path.name} ({path.stat().st_size} bytes)")


def describe(name, body, depth=0):
    """Print a shallow outline of a response so its shape is obvious."""
    prefix = "  " * (depth + 1)
    if isinstance(body, dict):
        for key, value in list(body.items())[:25]:
            kind = type(value).__name__
            if isinstance(value, (dict, list)):
                print(f"{prefix}{key}: {kind}[{len(value)}]")
                if depth < 1 and value:
                    sample = value[0] if isinstance(value, list) else value
                    describe(key, sample, depth + 1)
            else:
                print(f"{prefix}{key}: {kind} = {str(value)[:70]}")
    elif isinstance(body, list):
        print(f"{prefix}list[{len(body)}]")
        if body:
            describe(name, body[0], depth + 1)


def main() -> int:
    settings = get_settings()
    secrets = [settings.viewpoint_pass, settings.viewpoint_user]

    with ViewpointClient(settings) as client:
        print(f"client_ver={client.client_ver} api_key_id={client.api_key_id}")

        print("\n== listing/newtoday ==")
        try:
            body = client.new_today()
            save("newtoday", body, secrets)
            describe("newtoday", body)
        except ViewpointError as exc:
            print(f"  failed: {exc}")
            return 1

        listing_id = sys.argv[1] if len(sys.argv) > 1 else None
        if not listing_id:
            listing_id = find_listing_id(body)
        if not listing_id:
            print("\nCould not find a listing id in the newtoday response")
            return 1
        print(f"\nUsing listing_id={listing_id}")

        for name, call in (
            ("photos", lambda: client.listing_photos(listing_id)),
            ("cutsheet", lambda: client.listing_cutsheet(listing_id)),
        ):
            print(f"\n== listing/{name} ==")
            try:
                result = call()
                save(name, result, secrets)
                describe(name, result)
            except ViewpointError as exc:
                print(f"  failed: {exc}")

    return 0


def find_listing_id(body):
    """Dig the first listing-looking id out of an arbitrary response."""
    stack = [body]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            for key in ("listing_id", "listingid", "mls", "id"):
                value = node.get(key)
                if isinstance(value, (str, int)) and str(value).isdigit() and len(str(value)) >= 8:
                    return str(value)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
