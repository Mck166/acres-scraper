"""Selenium transport.

This is the fallback for when viewpoint's JSON API is unavailable. It drives a
headless browser the way the original scraper did, but reads photos out of the
page markup instead of clicking through the gallery widget.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .config import Settings, get_settings
from .cutsheet import parse_bootstrap, parse_description
from .identity import parse_cutsheet_url
from .photos import extract_photo_set, normalize_photo_list, photo_urls_for

log = logging.getLogger(__name__)


def build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(options=options)


class SeleniumClient:
    """Browser-driven access to viewpoint."""

    def __init__(self, settings: Optional[Settings] = None, driver=None):
        self.settings = settings or get_settings()
        self.driver = driver
        self._owns_driver = driver is None

    def __enter__(self) -> "SeleniumClient":
        if self.driver is None:
            self.driver = build_driver()
        self.login()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self.driver is not None and self._owns_driver:
            self.driver.quit()
            self.driver = None

    # -- session ---------------------------------------------------------

    def login(self) -> None:
        base = self.settings.viewpoint_base_url
        self.driver.get(f"{base}/user/login")
        wait = WebDriverWait(self.driver, 20)

        email = wait.until(EC.element_to_be_clickable((By.NAME, "login-email")))
        email.clear()
        email.send_keys(self.settings.viewpoint_user)

        password = wait.until(EC.element_to_be_clickable((By.NAME, "login-password")))
        password.clear()
        password.send_keys(self.settings.viewpoint_pass)
        password.send_keys(Keys.RETURN)

        time.sleep(3)
        if "login" in self.driver.current_url.lower():
            log.warning("Still on the login page after submitting credentials")
        else:
            log.info("Logged in to viewpoint")

    # -- listing discovery -----------------------------------------------

    def new_today_urls(self) -> List[str]:
        """Cutsheet URLs from the new-today activity list."""
        self.driver.get(f"{self.settings.viewpoint_base_url}/user#!/new-today-list/")
        time.sleep(5)

        urls: List[str] = []
        seen = set()
        for link in self.driver.find_elements(By.TAG_NAME, "a"):
            href = link.get_attribute("href")
            if not href or "cutsheet" not in href.lower():
                continue
            if href not in seen:
                seen.add(href)
                urls.append(href)

        log.info("Found %d cutsheet links on the new-today list", len(urls))
        return urls

    # -- listing detail --------------------------------------------------

    def fetch_listing(self, cutsheet_url: str) -> Optional[Dict[str, Any]]:
        """Scrape one cutsheet page into a raw property dict."""
        try:
            self.driver.get(cutsheet_url)
            time.sleep(3)
        except Exception as exc:
            log.error("Could not open %s: %s", cutsheet_url, exc)
            return None

        self._expand_details()

        data: Dict[str, Any] = {"url": cutsheet_url}
        data["Price"] = self._text_of(By.CLASS_NAME, "overlay-price")
        data["Status"] = self._text_of(By.CLASS_NAME, "overlay-status")
        data["Address"] = self._text_of(By.CLASS_NAME, "cutsheet-address")

        data.update(self._detail_items())

        try:
            html = self.driver.page_source
        except Exception:
            html = ""
        description = parse_description(html)
        if description:
            data["Description"] = description

        bootstrap = parse_bootstrap(html)
        if bootstrap.get("status_id") is not None:
            data["status_id"] = str(bootstrap["status_id"])

        photos = self.extract_photos(cutsheet_url)
        data["Photos"] = photos
        data["Photo_Count"] = len(photos)

        return data

    def _expand_details(self) -> None:
        try:
            section = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//div[@class='cutsheet-section-title' and contains(text(), 'Details')]")
                )
            )
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", section)
            time.sleep(0.5)
            try:
                section.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", section)
            time.sleep(1)
        except Exception as exc:
            log.debug("Details section not expandable: %s", exc)

    def _text_of(self, by: str, value: str) -> str:
        try:
            return self.driver.find_element(by, value).text.strip()
        except Exception:
            return ""

    def _detail_items(self) -> Dict[str, str]:
        items = self.driver.find_elements(By.CLASS_NAME, "cutsheet-detail-item")
        details: Dict[str, str] = {}

        for item in items:
            try:
                full_text = item.text.strip()
            except Exception:
                continue
            if not full_text:
                continue

            label: Optional[str]
            value: str
            try:
                span = item.find_element(By.TAG_NAME, "span")
                value = span.text.strip()
                label = full_text.replace(value, "").strip().rstrip(":").strip()
            except Exception:
                if ":" in full_text:
                    label, _, value = full_text.partition(":")
                    label = label.strip()
                    value = value.strip()
                else:
                    label, value = full_text, ""

            if label:
                details[label] = value

        return details

    # -- photos ----------------------------------------------------------

    def extract_photos(self, cutsheet_url: str) -> List[str]:
        """Build the photo set from the page source.

        The page only renders the first four photos, so the set is derived from
        the total the page advertises rather than from the image tags present.
        """
        try:
            html = self.driver.page_source
        except Exception as exc:
            log.error("Could not read page source for %s: %s", cutsheet_url, exc)
            return []

        parsed = parse_cutsheet_url(cutsheet_url)
        if not parsed:
            return []

        listing_id, class_id = parsed
        photo_set = extract_photo_set(html, listing_id, class_id)
        if photo_set is None:
            return []

        return normalize_photo_list(photo_urls_for(photo_set, self.settings.viewpoint_base_url))
