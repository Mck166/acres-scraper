"""Strip account details and session tokens out of recorded API fixtures.

    python tools/sanitize_fixtures.py

Viewpoint echoes the signed-in user and a fresh nonce on every response. Neither
is needed to test against, and neither belongs in version control.
"""

import json
import pathlib

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "viewpoint"

DROP_KEYS = {"api_user", "nonce", "api_login", "requested_at"}


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in DROP_KEYS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def main() -> int:
    if not FIXTURE_DIR.exists():
        print(f"No fixtures at {FIXTURE_DIR}")
        return 0

    for path in sorted(FIXTURE_DIR.glob("*.json")):
        before = path.stat().st_size
        body = json.loads(path.read_text())
        path.write_text(json.dumps(scrub(body), indent=2, sort_keys=True, default=str))
        print(f"  {path.name}: {before} -> {path.stat().st_size} bytes")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
