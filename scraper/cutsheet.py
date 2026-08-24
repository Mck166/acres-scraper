"""Parser for viewpoint cutsheet pages.

A cutsheet is server-rendered and carries everything we need, in three places:

* ``vp.initCutsheet({...})`` in a script tag, holding the listing's structured
  record: prices, dates, status, coordinates, bed and bath counts, photo count.
* ``<div class="cutsheet-detail-item">Roof:<span>Metal</span></div>`` blocks
  holding the MLS detail fields the app's detail screen renders.
* ``<span class="full-description">...</span>``, the marketing copy shown on
  the listing. It is not a detail item and is not in the bootstrap JSON.

The detail labels are Title Case in the markup but uppercased by CSS, and the
original scraper stored what the browser displayed. The uppercase form is what
the app and website read, so labels are uppercased here to match.
"""

import json
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

INIT_CUTSHEET_MARKER = "vp.initCutsheet("

# Labels the site renders differently from how they are stored.
LABEL_OVERRIDES = {
    "APPLIANCES INCL": "APPLIANCES INCL.",
    "PROV PARCEL SIZE": "PROV. PARCEL SIZE",
}


def _extract_balanced_json(text: str, start: int) -> Optional[str]:
    """Return the JSON object beginning at ``start``, respecting nesting."""
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return None


def parse_bootstrap(html: str) -> Dict[str, Any]:
    """Read the structured listing record the page hands to its JavaScript."""
    if not html:
        return {}

    marker = html.find(INIT_CUTSHEET_MARKER)
    if marker == -1:
        return {}

    brace = html.find("{", marker)
    if brace == -1:
        return {}

    raw = _extract_balanced_json(html, brace)
    if not raw:
        return {}

    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _clean_label(text: str) -> str:
    label = re.sub(r"\s+", " ", text).strip().rstrip(":").strip().upper()
    return LABEL_OVERRIDES.get(label, label)


def parse_detail_items(html: str) -> Dict[str, str]:
    """Pull the MLS detail fields out of the page."""
    if not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    details: Dict[str, str] = {}

    for item in soup.select("div.cutsheet-detail-item"):
        value_node = item.find("span")
        if value_node is None:
            continue

        value = re.sub(r"\s+", " ", value_node.get_text(" ", strip=True)).strip()

        # The label is whatever precedes the value span.
        label_text = "".join(
            str(node.get_text(" ", strip=True)) if hasattr(node, "get_text") else str(node)
            for node in value_node.previous_siblings
        )
        label = _clean_label(label_text)

        # Unrendered handlebars templates share these class names.
        if not label or "{{" in label or "{{" in value:
            continue

        details[label] = value

    return details


def parse_overlay(html: str) -> Dict[str, str]:
    """Read the price, status, and address shown over the hero photo."""
    if not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    overlay: Dict[str, str] = {}

    for key, selector in (
        ("Price", ".overlay-price"),
        ("Status", ".overlay-status"),
        ("Address", ".cutsheet-address"),
    ):
        node = soup.select_one(selector)
        if node is not None:
            overlay[key] = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()

    return overlay


def clean_description(value: Any) -> str:
    """Collapse whitespace and drop empty or unrendered template text."""
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text or "{{" in text:
        return ""
    return text


def description_from_api(body: Any) -> str:
    """Read the marketing copy out of a ``listing/cutsheet`` JSON response."""
    if not isinstance(body, dict):
        return ""
    cutsheet = body.get("cutsheet")
    if isinstance(cutsheet, dict):
        return clean_description(cutsheet.get("description"))
    return clean_description(body.get("description"))


def _description_from_json_ld(soup: BeautifulSoup) -> str:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            text = clean_description(item.get("description"))
            if text:
                return text
    return ""


def parse_description(html: str) -> str:
    """Read the listing's marketing description from the cutsheet page.

    Prefers the full description span the page shows after "Read more". The
    bootstrap record does not carry this field, and it is not a detail item.
    Schema.org JSON-LD is a fallback for pages that omit the span.
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(".full-description")
    if node is not None:
        text = clean_description(node.get_text(" ", strip=True))
        if text:
            return text

    return _description_from_json_ld(soup)


def parse_cutsheet(html: str) -> Dict[str, Any]:
    """Parse a cutsheet page into its bootstrap record, details, and description."""
    return {
        "bootstrap": parse_bootstrap(html),
        "details": parse_detail_items(html),
        "overlay": parse_overlay(html),
        "description": parse_description(html),
    }
