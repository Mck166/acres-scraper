"""Dump a cutsheet page for inspection.

    python tools/dump_cutsheet.py [cutsheet_url]

Writes the rendered HTML and the captured network log to output/debug/ so the
photo markup and the underlying API calls can be inspected offline.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper.config import get_settings  # noqa: E402
from scraper.selenium_client import SeleniumClient, build_driver  # noqa: E402

OUTPUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "output" / "debug"


def main() -> int:
    settings = get_settings()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    from selenium.webdriver.chrome.options import Options
    from selenium import webdriver

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    client = SeleniumClient(settings, driver=driver)

    try:
        client.login()

        url = sys.argv[1] if len(sys.argv) > 1 else None
        if not url:
            url = client.new_today_urls()[0]
        print(f"Dumping {url}")

        driver.get(url)
        import time

        time.sleep(6)

        (OUTPUT_DIR / "cutsheet.html").write_text(driver.page_source)
        print(f"  wrote cutsheet.html ({len(driver.page_source)} bytes)")

        entries = []
        for entry in driver.get_log("performance"):
            try:
                message = json.loads(entry["message"])["message"]
            except (KeyError, ValueError):
                continue
            method = message.get("method", "")
            if method in ("Network.requestWillBeSent", "Network.responseReceived"):
                entries.append(message)
        (OUTPUT_DIR / "network.json").write_text(json.dumps(entries, indent=2))
        print(f"  wrote network.json ({len(entries)} events)")

        return 0
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
