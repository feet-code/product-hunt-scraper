from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_GEMINI_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash",
    "gemini-2.5-flash",
)

DEFAULT_PRODUCT_SITEMAP = (
    "https://www.producthunt.com/sitemaps_v3/product_about_sitemap.xml.gz"
)
DEFAULT_USER_AGENT = (
    "MagicCatalogResearchBot/1.0 "
    "(+https://magic-catalog.cloudwebsites.workers.dev)"
)


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load a small, dependency-free subset of dotenv syntax."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def configured_models() -> tuple[str, ...]:
    raw = os.getenv("GEMINI_MODELS", "").strip()
    if not raw:
        return DEFAULT_GEMINI_MODELS
    values = tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    if not values:
        raise ValueError("GEMINI_MODELS did not contain any model IDs.")
    if len(values) > 6:
        raise ValueError("GEMINI_MODELS may contain at most six model IDs.")
    return values


def configured_gemini_api_keys() -> tuple[str, ...]:
    """Return authorized Gemini keys without logging or persisting their values."""
    values: list[str] = []
    raw_pool = os.getenv("GEMINI_API_KEYS", "").strip()
    if raw_pool:
        values.extend(item.strip() for item in raw_pool.split(",") if item.strip())
    legacy = os.getenv("GEMINI_API_KEY", "").strip()
    if legacy:
        values.append(legacy)
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class Settings:
    state_path: Path
    preview_path: Path
    sitemap_url: str
    user_agent: str
    gemini_api_key: str
    gemini_models: tuple[str, ...]
    magic_catalog_url: str
    magic_catalog_import_token: str
    product_hunt_delay_seconds: float
    product_hunt_jitter_seconds: float
    external_delay_seconds: float
    external_jitter_seconds: float
    request_timeout_seconds: float
    max_http_attempts: int

    @property
    def gemini_api_keys(self) -> tuple[str, ...]:
        configured = configured_gemini_api_keys()
        if configured:
            return configured
        return (self.gemini_api_key,) if self.gemini_api_key else ()

    @classmethod
    def from_environment(
        cls,
        *,
        state_path: Path,
        preview_path: Path,
        sitemap_url: str,
        product_hunt_delay_seconds: float,
        product_hunt_jitter_seconds: float,
        external_delay_seconds: float,
        external_jitter_seconds: float,
        request_timeout_seconds: float,
        max_http_attempts: int,
    ) -> Settings:
        gemini_api_keys = configured_gemini_api_keys()
        return cls(
            state_path=state_path,
            preview_path=preview_path,
            sitemap_url=sitemap_url,
            user_agent=os.getenv("SCRAPER_USER_AGENT", DEFAULT_USER_AGENT).strip()
            or DEFAULT_USER_AGENT,
            gemini_api_key=gemini_api_keys[0] if gemini_api_keys else "",
            gemini_models=configured_models(),
            magic_catalog_url=os.getenv(
                "MAGIC_CATALOG_URL",
                "https://magic-catalog.cloudwebsites.workers.dev",
            ).strip().rstrip("/"),
            magic_catalog_import_token=os.getenv(
                "MAGIC_CATALOG_IMPORT_TOKEN", ""
            ).strip(),
            product_hunt_delay_seconds=max(2.0, product_hunt_delay_seconds),
            product_hunt_jitter_seconds=max(0.0, product_hunt_jitter_seconds),
            external_delay_seconds=max(1.0, external_delay_seconds),
            external_jitter_seconds=max(0.0, external_jitter_seconds),
            request_timeout_seconds=max(5.0, request_timeout_seconds),
            max_http_attempts=max(1, min(max_http_attempts, 8)),
        )
