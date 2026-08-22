"""Environment-driven configuration for the scraper."""

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    viewpoint_user: str
    viewpoint_pass: str
    viewpoint_base_url: str

    mongodb_uri: str
    mongodb_db_name: str
    properties_collection: str
    sold_collection: str
    recent_updates_collection: str
    geocode_cache_collection: str
    scrape_runs_collection: str
    locks_collection: str

    transport: str
    stale_recheck_limit: int
    recent_updates_ttl_seconds: int
    request_delay_seconds: float

    acres_api_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            viewpoint_user=os.getenv("VIEWPOINT_USER", ""),
            viewpoint_pass=os.getenv("VIEWPOINT_PASS", ""),
            viewpoint_base_url=os.getenv(
                "VIEWPOINT_BASE_URL", "https://www.viewpoint.ca"
            ).rstrip("/"),
            mongodb_uri=os.getenv("MONGODB_URI", "mongodb://localhost:27017/"),
            mongodb_db_name=os.getenv("MONGODB_DB_NAME", "viewpoint_properties"),
            properties_collection=os.getenv("MONGODB_COLLECTION_NAME", "properties"),
            sold_collection=os.getenv("MONGODB_SOLD_COLLECTION_NAME", "sold_properties"),
            recent_updates_collection=os.getenv(
                "MONGODB_RECENT_UPDATES_COLLECTION_NAME", "recent_updates"
            ),
            geocode_cache_collection=os.getenv(
                "MONGODB_GEOCODE_CACHE_COLLECTION_NAME", "geocode_cache"
            ),
            scrape_runs_collection=os.getenv(
                "MONGODB_SCRAPE_RUNS_COLLECTION_NAME", "scrape_runs"
            ),
            locks_collection=os.getenv("MONGODB_LOCKS_COLLECTION_NAME", "scraper_locks"),
            transport=os.getenv("SCRAPER_TRANSPORT", "api").strip().lower(),
            stale_recheck_limit=_int_env("STALE_RECHECK_LIMIT", 25),
            recent_updates_ttl_seconds=_int_env("RECENT_UPDATES_TTL_SECONDS", 86400),
            request_delay_seconds=float(os.getenv("VIEWPOINT_REQUEST_DELAY", "0.35")),
            acres_api_url=os.getenv("ACRES_API_URL", "").rstrip("/"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def reset_settings() -> None:
    """Drop the cached settings so a later call re-reads the environment."""
    get_settings.cache_clear()
