"""Find which API call populates a cutsheet's Details section.

    python tools/probe_details_section.py [cutsheet_url]

The detail fields the app shows are not in the server-rendered page; expanding
the Details accordion fetches them. This clicks that accordion with network
logging on and reports the requests it triggered, so the scraper can call the
same endpoint directly.
"""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from selenium import webdriver  # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402

from scraper.config import get_settings  # noqa: E402
from scraper.selenium_client import SeleniumClient  # noqa: E402


def api_requests(driver):
    seen = []
    for entry in driver.get_log("performance"):
        try:
            message = json.loads(entry["message"])["message"]
        except (KeyError, ValueError):
            continue
        if message.get("method") != "Network.requestWillBeSent":
            continue
        url = message["params"]["request"]["url"]
        if "/api/v" in url:
            seen.append(url)
    return seen


def main() -> int:
    settings = get_settings()

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    client = SeleniumClient(settings, driver=driver)

    try:
        client.login()
        url = sys.argv[1] if len(sys.argv) > 1 else client.new_today_urls()[0]
        print(f"Opening {url}")
        driver.get(url)
        time.sleep(5)

        api_requests(driver)  # drain everything from the initial load

        print("\nClicking the Details section...")
        clicked = False
        for element in driver.find_elements(By.CLASS_NAME, "cutsheet-section-title"):
            if "detail" in element.text.strip().lower():
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                driver.execute_script("arguments[0].click();", element)
                clicked = True
                break
        if not clicked:
            print("  no Details section found")
            return 1

        time.sleep(4)

        print("\nRequests triggered by the click:")
        for request in api_requests(driver):
            print(f"  {request[:220]}")

        items = driver.find_elements(By.CLASS_NAME, "cutsheet-detail-item")
        visible = [i.text.strip() for i in items if i.text.strip()]
        print(f"\nVisible detail items after expanding: {len(visible)}")
        for item in visible[:40]:
            print(f"  {item[:90]!r}")

        return 0
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
