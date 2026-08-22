"""Parser for viewpoint cutsheet pages.

A cutsheet is server-rendered and carries everything we need, in two places:

* ``vp.initCutsheet({...})`` in a script tag, holding the listing's structured
  record: prices, dates, status, coordinates, bed and bath counts, photo count.
* ``<div class="cutsheet-detail-item">Roof:<span>Metal</span></div>`` blocks
  holding the MLS detail fields the app's detail screen renders.

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


def parse_cutsheet(html: str) -> Dict[str, Any]:
    """Parse a cutsheet page into its bootstrap record and detail fields."""
    return {
        "bootstrap": parse_bootstrap(html),
        "details": parse_detail_items(html),
        "overlay": parse_overlay(html),
    }
